"""The fixes from the first real PAPER day (2026-11-09 review), on SYNTHETIC data:
candle backfill and repair, close-order P&L, stricter warm-up, conservative stop
fills, signal-time features, market recording, decision logs, frozen-price gate."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from config.loader import load_config
from config.settings import MarketRecordingSection
from core.enums import EvidenceSource, OptionType, OrderSide, OrderType, SnapshotPurpose, TradeStatus, TradingMode
from core.event_bus import Topic
from core.models import Candle, Tick
from data.candle_builder import CandleBuilder
from data.market_data import MarketDataService
from data.storage.database import SCHEMA_VERSION, Database
from data.storage.records import FeatureSnapshotRecord, TradeJournalRecord
from data.storage.repositories import Repositories
from learning.reflection_engine import ReflectionEngine, entry_features, moneyness_bucket
from learning.trade_journal import TradeFacts, build_entry
from risk.daily_loss_guard import DailyLossGuard
from risk.stop_loss_manager import ExitReason
from runtime.history import PRIOR_DAY_ATTEMPTS, REPAIR_ATTEMPTS, HistoryLoader
from runtime.market_recorder import MarketRecorder
from runtime.paper import FeedHealthMonitor
from strategies.indicators import EMA, SessionVWAP
from strategies.orb_options import OrbOptionsStrategy
from strategies.vwap_ema import VwapEmaStrategy
from tests.fakes import FakeWallClock
from tests.paper_support import FakeConnections, FakeFeed, drive, paper_cfg, session
from tests.stack_harness import CE_KEY, Harness
from tests.synthetic import FUT, INDEX, at, candle, five_min, opening_range_candles, tick
from tests.unit.test_bot_integration import ORB_DAY, NoSource, Sim, no_sleep
from tests.unit.test_paper_broker import Env, req

CFG = load_config(use_default_env_file=False)
ORB_CFG = CFG.strategies.strategies["orb_options"]
VWAP_CFG = CFG.strategies.strategies["vwap_ema"]
DAY = date(2026, 11, 3)


def vwap_strategy() -> VwapEmaStrategy:
    s = VwapEmaStrategy("vwap_ema", VWAP_CFG, signal_instrument_key=FUT)
    s.reset_day(DAY)
    return s


def orb_strategy() -> OrbOptionsStrategy:
    s = OrbOptionsStrategy("orb_options", ORB_CFG, signal_instrument_key=INDEX)
    s.reset_day(DAY)
    return s


def rising(n: int, start: float = 24500.0, step: float = 10.0) -> list[float]:
    return [start + i * step for i in range(n)]


class HistorySource(NoSource):
    """The Upstox candle APIs, serving SYNTHETIC candles."""

    def __init__(self, data: dict[tuple[str, int], list[Candle]] | None = None,
                 prior: dict[date, list[Candle]] | None = None) -> None:
        super().__init__()
        self.data = data or {}
        self.prior = prior or {}
        self.fail = False
        self.fail_prior = False
        self.requests: list[tuple[str, int]] = []
        self.prior_requests: list[date] = []

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        self.requests.append((instrument_key, interval_minutes))
        if self.fail:
            raise ConnectionError("candle API down")
        return self.data.get((instrument_key, interval_minutes), [])

    async def get_historical_candles(self, instrument_key: str, interval_minutes: int, from_date: date,
                                     to_date: date) -> list[Candle]:
        self.prior_requests.append(to_date)
        if self.fail_prior:
            raise ConnectionError("candle API down")
        return self.prior.get(to_date, [])


def fast_cfg(tmp_path: Path) -> Any:
    """A PAPER config whose candle retries do not wait."""
    cfg = paper_cfg(tmp_path)
    ws = cfg.settings.websocket.model_copy(update={"candle_repair_delay_seconds": 0.0})
    return cfg.model_copy(update={"settings": cfg.settings.model_copy(update={"websocket": ws})})


def paused(bot: Any, sid: str) -> str | None:
    return bot.stack.guard.state[sid].paused_reason  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# 1. Backfill at start-up
# --------------------------------------------------------------------------- #

#: complete candles at 12:41: 41 of 5 minutes (09:15 to 12:35), 206 of 1 minute
BARS_5M, BARS_1M = 41, 206


def one_minute(key: str, start: int = 0, volume: int = 65) -> list[Candle]:
    return [candle(key, 1, at(9, 15) + timedelta(minutes=i), 24500, 24501, 24499, 24500, volume)
            for i in range(start, BARS_1M)]


def morning(volumes: int = 10_000) -> dict[tuple[str, int], list[Candle]]:
    """A complete SYNTHETIC session up to 12:41."""
    return {(FUT, 5): five_min(FUT, at(9, 15), rising(BARS_5M), [volumes] * BARS_5M),
            (INDEX, 1): [*opening_range_candles(INDEX, high=24520, low=24480), *one_minute(INDEX, 15, 0)],
            (INDEX, 5): five_min(INDEX, at(9, 15), [24500.0] * BARS_5M),
            (FUT, 1): one_minute(FUT)}


async def test_backfill_gives_strategies_the_same_state_as_a_full_session(tmp_path: Path) -> None:
    wall = FakeWallClock(at(12, 41))
    source = HistorySource(morning())
    s = session(paper_cfg(tmp_path), wall, FakeConnections(wall, source))
    bot = await s.start()
    try:
        strategies = {x.strategy_id: x for x in bot.strategies}
        got, orb = strategies["vwap_ema"], strategies["orb_options"]
        assert isinstance(got, VwapEmaStrategy) and isinstance(orb, OrbOptionsStrategy)

        reference = vwap_strategy()
        for c in morning()[(FUT, 5)]:
            reference.on_candle(c)
        assert paused(bot, "vwap_ema") is None and paused(bot, "orb_options") is None
        assert got.ema.count == BARS_5M and got.ema.ready and got.ema.value == pytest.approx(reference.ema.value)
        assert got.vwap.value == pytest.approx(reference.vwap.value)  # a true session VWAP, from 09:15
        assert got.volume.mean == reference.volume.mean and list(got.sides) == list(reference.sides)

        assert orb.snapshot()["opening_range"] == {"high": 24520, "low": 24480, "ready": True, "invalid": None,
                                                   "minutes": 15}
        assert bot.learning is not None and bot.learning.features.last_bar_end == at(12, 41)

        assert bot.engine is not None
        assert bot.engine.state.strategy("vwap_ema").signals == 0  # replay cannot signal
        assert got.signals_today == 0 and orb.signals_today == 0
        assert set(source.requests) == {(FUT, 5), (INDEX, 1), (INDEX, 5), (FUT, 1)}
    finally:
        await s.shutdown()


async def test_backfill_ignores_forming_other_day_and_misaligned_candles() -> None:
    sim = Sim(at(12, 41))
    await sim.bot.prepare()
    good = five_min(FUT, at(9, 15), rising(BARS_5M))
    forming = five_min(FUT, at(12, 40), [24500.0])[0].model_copy(update={"complete": False})
    not_ended = five_min(FUT, at(12, 40), [24500.0])[0]  # marked complete, but ends after now
    yesterday = five_min(FUT, at(9, 15, day=(2026, 11, 2)), [24500.0])[0]
    misaligned = five_min(FUT, at(9, 17), [24500.0])[0]
    sim.bot.source = HistorySource({(FUT, 5): [yesterday, *good, misaligned, forming, not_ended]})
    await HistoryLoader(sim.bot, sleep=no_sleep).backfill_today()
    strat = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    assert strat.ema.count == BARS_5M


async def test_a_failed_backfill_blocks_entries_for_the_day(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    wall = FakeWallClock(at(12, 41))
    source = HistorySource()
    source.fail = True
    s = session(fast_cfg(tmp_path), wall, FakeConnections(wall, source))
    with caplog.at_level(logging.WARNING):
        bot = await s.start()
    try:
        assert any(r.getMessage() == "candle backfill incomplete" for r in caplog.records)
        assert "candle backfill incomplete" in (paused(bot, "vwap_ema") or "")
        assert "candle backfill incomplete" in (paused(bot, "orb_options") or "")
        strat = next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy))
        assert strat.ema.count == 0  # nothing partial reached the indicators
        assert bot.engine is not None and bot.engine.accepting_signals  # the session itself carries on
        assert bot.stack.guard.snapshot()["strategies"]["vwap_ema"]["paused"]  # type: ignore[index] # dashboard
    finally:
        await s.shutdown()


async def _sim_backfill(data: dict[tuple[str, int], list[Candle]], now: datetime | None = None) \
        -> tuple[Sim, HistorySource, HistoryLoader]:
    sim = Sim(now or at(12, 41))
    await sim.bot.prepare()
    source = HistorySource(data)
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.backfill_today()
    return sim, source, loader


async def test_an_empty_answer_counts_as_a_failed_backfill() -> None:
    sim, _, _ = await _sim_backfill({})  # Upstox answered, but with no candles
    assert paused(sim.bot, "vwap_ema") and paused(sim.bot, "orb_options")


async def test_one_missing_candle_blocks_only_the_strategy_that_needs_it() -> None:
    data = morning()
    del data[(FUT, 5)][7]
    sim, _, _ = await _sim_backfill(data)
    reason = paused(sim.bot, "vwap_ema")
    assert reason is not None and "1 candles missing, first 09:50" in reason
    assert paused(sim.bot, "orb_options") is None  # isolation
    vw = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    orb = next(x for x in sim.bot.strategies if isinstance(x, OrbOptionsStrategy))
    assert vw.ema.count == 0 and orb.range_ready  # the blocked one got none of its partial history


async def test_a_gap_in_features_only_data_blocks_nothing() -> None:
    data = morning()
    del data[(FUT, 1)]  # only the feature engine reads 1-minute futures
    sim, _, _ = await _sim_backfill(data)
    assert paused(sim.bot, "vwap_ema") is None and paused(sim.bot, "orb_options") is None


async def test_a_candle_upstox_has_not_published_yet_is_retried() -> None:
    data = morning()
    late = data[(FUT, 5)].pop()  # the 12:35 candle is not out yet at 12:40:02

    class Late(HistorySource):
        async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
            got = await super().get_intraday_candles(instrument_key, interval_minutes)
            if instrument_key == FUT and interval_minutes == 5 and self.requests.count((FUT, 5)) >= 2:
                return [*got, late]
            return got

    sim = Sim(at(12, 40, 2))
    await sim.bot.prepare()
    source = Late({k: [c for c in v if c.end <= at(12, 40)] for k, v in data.items()})
    sim.bot.source = source
    await HistoryLoader(sim.bot, sleep=no_sleep).backfill_today()
    assert source.requests.count((FUT, 5)) == 2 and paused(sim.bot, "vwap_ema") is None


async def test_a_late_newest_candle_blocks_only_the_series_indicators_run_on() -> None:
    data = morning()
    data[(FUT, 5)].pop()  # the 12:35 candle never arrives
    data[(INDEX, 1)].pop()  # nor does the 12:40 one-minute candle
    sim, _, _ = await _sim_backfill(data)
    assert "1 candles missing, first 12:35" in (paused(sim.bot, "vwap_ema") or "")
    assert paused(sim.bot, "orb_options") is None  # ORB's range finished long ago


async def test_a_hole_inside_the_opening_range_still_stops_orb() -> None:
    data = morning()
    del data[(INDEX, 1)][7]
    sim, _, _ = await _sim_backfill(data)
    assert "candles missing" in (paused(sim.bot, "orb_options") or "")


async def test_a_real_hole_is_not_retried() -> None:
    data = morning()
    del data[(FUT, 5)][3:6]
    _, source, _ = await _sim_backfill(data)
    assert source.requests.count((FUT, 5)) == 1


async def test_a_transient_error_is_retried() -> None:
    data = morning()

    class Flaky(HistorySource):
        async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
            if not self.requests.count((instrument_key, interval_minutes)):
                self.requests.append((instrument_key, interval_minutes))
                raise ConnectionError("blip")
            return await super().get_intraday_candles(instrument_key, interval_minutes)

    sim = Sim(at(12, 41))
    await sim.bot.prepare()
    sim.bot.source = Flaky(data)
    await HistoryLoader(sim.bot, sleep=no_sleep).backfill_today()
    assert paused(sim.bot, "vwap_ema") is None and paused(sim.bot, "orb_options") is None


async def test_no_backfill_after_the_close() -> None:
    sim, source, _ = await _sim_backfill({}, at(16, 0))
    assert source.requests == [] and paused(sim.bot, "vwap_ema") is None


def _flaky_generate(monkeypatch: pytest.MonkeyPatch, cls: Any, fail_on: int) -> None:
    real, calls = cls.generate, [0]

    def flaky(self: Any, c: Candle) -> Any:
        calls[0] += 1
        if calls[0] == fail_on:
            raise ValueError("bad candle")
        return real(self, c)

    monkeypatch.setattr(cls, "generate", flaky)


def test_replay_is_all_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    s = vwap_strategy()
    _flaky_generate(monkeypatch, VwapEmaStrategy, 5)
    with pytest.raises(ValueError, match="bad candle"):
        s.replay(five_min(FUT, at(9, 15), rising(10), [1000] * 10))
    assert s.ema.count == 0 and s.ema.value is None and s.vwap.value is None and len(s.recent) == 0
    assert s.volume.mean is None and s._last_end == {} and not s.replaying


async def test_a_replay_error_blocks_only_that_strategy(monkeypatch: pytest.MonkeyPatch,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    _flaky_generate(monkeypatch, VwapEmaStrategy, 5)
    with caplog.at_level(logging.ERROR):
        sim, _, _ = await _sim_backfill(morning())
    vw = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    orb = next(x for x in sim.bot.strategies if isinstance(x, OrbOptionsStrategy))
    assert vw.ema.count == 0 and "candle replay failed (ValueError)" in (paused(sim.bot, "vwap_ema") or "")
    assert orb.range_ready and paused(sim.bot, "orb_options") is None
    assert any(r.getMessage() == "candle replay failed" for r in caplog.records)


async def test_a_replay_error_during_repair_blocks_the_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    sim, source, loader = await _sim_backfill(morning())
    vw = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    bar = five_min(FUT, at(12, 40), [24900.0])[0]
    source.data[(FUT, 5)] = [*source.data[(FUT, 5)], bar]
    sim.wall.now = at(12, 45, 3)
    _flaky_generate(monkeypatch, VwapEmaStrategy, 1)
    loader.on_candle(bar.model_copy(update={"complete": False}))
    results = await asyncio.gather(*loader._tasks, return_exceptions=True)
    assert not [r for r in results if isinstance(r, BaseException)]  # nothing escapes the repair task
    assert vw.ema.count == BARS_5M and "candle replay failed" in (paused(sim.bot, "vwap_ema") or "")


async def test_a_restart_with_an_open_trade_backfills_without_touching_caps_or_the_stop(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    first = Sim(at(9, 14), db=Database(path))
    await first.bot.prepare()
    first.bot.start()
    await first.run_path(ORB_DAY)
    (before,) = first.bot.stack.positions.open_trades("orb_options")
    stop = before.stop
    first.db.close()

    restarted = Sim(at(9, 50, 30), db=Database(path))
    await restarted.bot.prepare()
    restarted.bot.start()
    source = HistorySource({k: [c for c in v if c.end <= at(9, 50)] for k, v in morning().items()})
    restarted.bot.source = source
    await HistoryLoader(restarted.bot, sleep=no_sleep).backfill_today()

    orb = next(x for x in restarted.bot.strategies if isinstance(x, OrbOptionsStrategy))
    assert orb.entries[OptionType.CE] == 1 and orb.range_ready and orb.signals_today == 0  # the cap survives replay
    assert paused(restarted.bot, "orb_options") is None
    (after,) = restarted.bot.stack.positions.open_trades("orb_options")
    assert after.trade_uid == before.trade_uid and after.stop == stop and after.protective_order_id is not None
    await restarted.step(at(9, 50, 40), 24565.0)  # the first index tick after the restart
    await restarted.step(at(9, 51), 24566.0, premium=70.0)  # the restored stop still protects the position
    rec = restarted.repos.trades.get(before.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"
    restarted.db.close()


async def test_a_real_partial_candle_is_built_flagged_and_repaired_end_to_end(tmp_path: Path) -> None:
    wall = FakeWallClock(at(12, 41, 12))
    data = {k: [c for c in v if c.end <= at(12, 40)] for k, v in morning().items()}
    source = HistorySource(data)
    conns = FakeConnections(wall, source)
    s = session(fast_cfg(tmp_path), wall, conns)
    bot = await s.start()
    try:
        assert conns.md is not None and s.history is not None and s.market_recorder is not None
        strat = next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy))
        assert strat.ema.count == BARS_5M and paused(bot, "vwap_ema") is None

        conns.md.on_tick(tick(FUT, 25000.0, wall.now, vtt=1000))  # started 1 min 12 s into the 12:40 bucket
        source.data[(FUT, 5)] = [*source.data[(FUT, 5)], five_min(FUT, at(12, 40), [25001.0], [9000])[0]]
        wall.now = at(12, 45, 0)
        conns.md.on_tick(tick(FUT, 25002.0, wall.now, vtt=2000))  # closes the partial candle
        await asyncio.gather(*s.history._tasks)

        assert strat.ema.count == BARS_5M + 1 and strat.recent[-1].close == 25001.0  # Upstox's full candle, not ours
        assert strat.recent[-1].volume == 9000 and paused(bot, "vwap_ema") is None
        s.market_recorder.flush()
        row = [r for r in bot.repos.market_history.candles(FUT, 5, DAY) if r["start_time"] == at(12, 40).isoformat()]
        assert (row[0]["complete"], row[0]["source"], row[0]["volume"]) == (1, "BACKFILL", 9000)
    finally:
        await s.shutdown()


async def test_a_blocked_strategy_is_refused_at_the_risk_gate_but_can_still_exit() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    sim.bot.stack.guard.block_entries_today("orb_options", "candle backfill incomplete (test)")
    await sim.run_path(ORB_DAY)
    assert sim.bot.stack.positions.open_trade_count("orb_options") == 0
    decisions = [e.payload for e in sim.bot.bus.recent(Topic.ENTRY_DECISION)]
    assert decisions and not any(d["approved"] for d in decisions)
    assert any("strategy paused: candle backfill incomplete" in " ".join(d["reasons"]) for d in decisions)


async def test_blocking_after_entry_leaves_the_open_trade_protected() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    sim.bot.stack.guard.block_entries_today("orb_options", "test")
    await sim.step(at(9, 51), 24566.0, premium=70.0)  # stop hit
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"


def test_a_block_lasts_for_the_day_and_is_not_persisted(paper: Repositories) -> None:
    wall = FakeWallClock(at(10, 0))
    limits = CFG.strategies.strategies
    guard = DailyLossGuard(CFG.risk, {k: v.risk for k, v in limits.items()}, repo=paper.daily_pnl, now_fn=wall)
    guard.block_entries_today("vwap_ema", "backfill failed")
    guard.block_entries_today("vwap_ema", "a later reason")  # the first reason stands
    allowed, reasons = guard.can_enter("vwap_ema")
    assert not allowed and reasons == ["strategy paused: backfill failed"]
    assert guard.can_enter("orb_options")[0]

    restarted = DailyLossGuard(CFG.risk, {k: v.risk for k, v in limits.items()}, repo=paper.daily_pnl, now_fn=wall)
    restarted.restore()
    assert restarted.can_enter("vwap_ema")[0]  # a restart re-checks the data

    wall.advance(days=1)
    assert guard.can_enter("vwap_ema")[0]  # gone the next day


async def test_no_backfill_call_before_the_market_opens() -> None:
    sim = Sim(at(9, 10))
    await sim.bot.prepare()
    source = HistorySource()
    sim.bot.source = source
    assert await HistoryLoader(sim.bot, sleep=no_sleep).backfill_today() == {}
    assert source.requests == []


async def _prepared_loader(until: datetime) -> tuple[Sim, HistorySource, HistoryLoader, VwapEmaStrategy]:
    sim = Sim(until)
    await sim.bot.prepare()
    source = HistorySource(morning())
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.backfill_today()
    strat = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    return sim, source, loader, strat


async def test_a_partial_candle_is_repaired_from_upstox_once() -> None:
    sim, source, loader, strat = await _prepared_loader(at(12, 41))
    assert strat.ema.count == BARS_5M
    full = five_min(FUT, at(12, 40), [24900.0])[0]
    source.data[(FUT, 5)] = [*source.data[(FUT, 5)], full]
    sim.wall.now = at(12, 45, 3)
    partial = full.model_copy(update={"complete": False})

    loader.on_candle(partial)
    await asyncio.gather(*loader._tasks)
    assert strat.ema.count == BARS_5M + 1 and paused(sim.bot, "vwap_ema") is None
    loader.on_candle(partial)  # a repeat cannot count a candle twice
    await asyncio.gather(*loader._tasks)
    assert strat.ema.count == BARS_5M + 1


async def test_an_unrepairable_partial_candle_blocks_that_strategy(caplog: pytest.LogCaptureFixture) -> None:
    sim, source, loader, strat = await _prepared_loader(at(12, 41))
    source.requests.clear()
    sim.wall.now = at(12, 45, 3)
    partial = five_min(FUT, at(12, 40), [24900.0])[0].model_copy(update={"complete": False})
    with caplog.at_level(logging.WARNING):
        loader.on_candle(partial)
        await asyncio.gather(*loader._tasks)
    assert len(source.requests) == REPAIR_ATTEMPTS and strat.ema.count == BARS_5M
    assert any("could not be repaired" in r.getMessage() for r in caplog.records)
    assert "could not be repaired" in (paused(sim.bot, "vwap_ema") or "")
    assert paused(sim.bot, "orb_options") is None


async def test_a_gap_after_the_backfill_blocks_that_strategy(caplog: pytest.LogCaptureFixture) -> None:
    sim, _, loader, _ = await _prepared_loader(at(12, 41))
    sim.wall.now = at(12, 50)
    with caplog.at_level(logging.WARNING):
        loader.on_candle(five_min(FUT, at(12, 45), [24900.0])[0])  # the backfill ended at 12:40
    assert any(r.getMessage() == "gap between candles" for r in caplog.records)
    assert "candle gap 12:40-12:45" in (paused(sim.bot, "vwap_ema") or "")


async def test_contiguous_candles_and_a_new_day_are_not_gaps() -> None:
    sim, _, loader, _ = await _prepared_loader(at(12, 41))
    loader.on_candle(five_min(FUT, at(12, 40), [24900.0])[0])
    loader.on_candle(five_min(FUT, at(12, 45), [24900.0])[0])
    loader.on_candle(five_min(FUT, at(9, 15, day=(2026, 11, 4)), [24900.0])[0])
    assert paused(sim.bot, "vwap_ema") is None


async def test_a_gap_in_a_series_a_strategy_does_not_run_on_blocks_nothing() -> None:
    sim, _, loader, _ = await _prepared_loader(at(12, 41))
    loader.on_candle(candle(INDEX, 1, at(12, 50), 24500, 24501, 24499, 24500))  # ORB's range is long finished
    assert paused(sim.bot, "orb_options") is None


async def test_a_paused_engine_keeps_indicators_current_without_signals() -> None:
    sim = Sim(at(10, 0))
    await sim.bot.prepare()
    sim.bot.pause()
    assert sim.bot.engine is not None
    strat = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    for c in five_min(FUT, at(9, 15), rising(3), [1000] * 3):
        sim.bot.engine.on_candle(c)
    assert strat.ema.count == 3 and strat.vwap.value is not None
    assert sim.bot.engine.state.strategy("vwap_ema").signals == 0


# --------------------------------------------------------------------------- #
# Seeding the EMA from the previous trading day
# --------------------------------------------------------------------------- #

MONDAY, FRIDAY = date(2026, 11, 2), date(2026, 10, 30)


def prior_day(day: date, n: int = 75, start: float = 24000.0) -> list[Candle]:
    return five_min(FUT, at(9, 15, day=(day.year, day.month, day.day)), rising(n, start, 1.0), [5000] * n)


async def _seed_sim(prior: dict[date, list[Candle]], now: datetime | None = None) -> tuple[Sim, HistorySource, Any]:
    sim = Sim(now or at(9, 10))
    await sim.bot.prepare()
    source = HistorySource(prior=prior)
    sim.bot.source = source
    await HistoryLoader(sim.bot, sleep=no_sleep).seed_prior_session()
    return sim, source, next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))


async def test_the_ema_is_ready_before_the_open_from_yesterdays_candles() -> None:
    sim, source, strat = await _seed_sim({MONDAY: prior_day(MONDAY)})
    assert strat.ema.ready and strat.ema.count == 75 and source.prior_requests == [MONDAY]
    reference = EMA(20)
    for c in prior_day(MONDAY):
        reference.update(c.close)
    assert strat.ema.value == pytest.approx(reference.value)
    assert strat.signals_today == 0 and paused(sim.bot, "vwap_ema") is None
    assert strat.prior_session_bars() == 0  # already seeded: asks for nothing more


async def test_vwap_still_resets_every_day() -> None:
    _, _, strat = await _seed_sim({MONDAY: prior_day(MONDAY)})
    assert strat.vwap.value is None  # yesterday's volume is not in it
    bar = five_min(FUT, at(9, 15), [24500.0], [1000])[0]
    strat.on_candle(bar)
    assert strat.vwap.value == pytest.approx((bar.high + bar.low + bar.close) / 3)
    assert strat.volume.mean == 1000


async def test_a_seeded_strategy_is_not_warming_up_on_the_third_candle() -> None:
    _, _, strat = await _seed_sim({MONDAY: prior_day(MONDAY)})
    for c in five_min(FUT, at(9, 15), [24500.0, 24500.0, 24500.0], [1000] * 3):
        strat.on_candle(c)
    assert strat.last_reason != "warming up"


async def test_the_seed_skips_the_weekend() -> None:
    sim, source, strat = await _seed_sim({FRIDAY: prior_day(FRIDAY)}, at(9, 10, day=(2026, 11, 2)))
    assert strat.ema.ready and source.prior_requests == [FRIDAY] and paused(sim.bot, "vwap_ema") is None


async def test_an_unlisted_holiday_falls_back_to_the_day_before() -> None:
    _, source, strat = await _seed_sim({FRIDAY: prior_day(FRIDAY)})  # Monday has no candles
    assert strat.ema.ready and source.prior_requests == [MONDAY, FRIDAY]


async def test_the_seed_refuses_too_few_candles() -> None:
    sim, _, strat = await _seed_sim({MONDAY: prior_day(MONDAY, 19)})
    assert not strat.ema.ready and strat.ema.count == 0
    assert paused(sim.bot, "vwap_ema") is None


async def test_no_seed_means_warming_up_not_a_block(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        sim, source, strat = await _seed_sim({})
    assert len(source.prior_requests) == PRIOR_DAY_ATTEMPTS
    assert not strat.ema.ready and paused(sim.bot, "vwap_ema") is None  # EMA not ready: no signal is possible
    assert any("no previous-session seed" in r.getMessage() for r in caplog.records)
    bars = five_min(FUT, at(9, 15), rising(10), [1000] * 9 + [9000])
    assert not [x for c in bars for x in strat.on_candle(c)] and strat.last_reason == "warming up"


async def test_a_seed_fetch_error_never_stops_the_session() -> None:
    sim = Sim(at(9, 10))
    await sim.bot.prepare()
    source = HistorySource()
    source.fail_prior = True
    sim.bot.source = source
    assert await HistoryLoader(sim.bot, sleep=no_sleep).seed_prior_session() == {}
    assert paused(sim.bot, "vwap_ema") is None


async def test_a_seed_error_rolls_back_and_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    real = VwapEmaStrategy.warm_up

    def half_way(self: Any, candles: Any) -> int:
        real(self, list(candles)[:5])
        raise ValueError("bad seed")

    monkeypatch.setattr(VwapEmaStrategy, "warm_up", half_way)
    sim, _, strat = await _seed_sim({MONDAY: prior_day(MONDAY)})
    assert strat.ema.count == 0 and strat.ema.value is None
    assert "previous-session seed failed (ValueError)" in (paused(sim.bot, "vwap_ema") or "")


async def test_only_strategies_with_a_cross_day_indicator_are_seeded() -> None:
    assert orb_strategy().prior_session_bars() == 0
    _, source, _ = await _seed_sim({MONDAY: prior_day(MONDAY)})
    assert len(source.prior_requests) == 1  # vwap_ema only


async def test_a_mid_day_restart_seeds_then_replays_today(tmp_path: Path) -> None:
    wall = FakeWallClock(at(12, 41))
    source = HistorySource(morning(), {MONDAY: prior_day(MONDAY)})
    s = session(fast_cfg(tmp_path), wall, FakeConnections(wall, source))
    bot = await s.start()
    try:
        got = next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy))
        reference = EMA(20)
        for c in [*prior_day(MONDAY), *morning()[(FUT, 5)]]:
            reference.update(c.close)
        assert got.ema.count == 75 + BARS_5M and got.ema.value == pytest.approx(reference.value)
        day_only = SessionVWAP()
        for c in morning()[(FUT, 5)]:
            day_only.update(c.start.date(), c.high, c.low, c.close, c.volume)
        assert got.vwap.value == pytest.approx(day_only.value)
    finally:
        await s.shutdown()


async def test_a_session_started_before_the_open_is_seeded(tmp_path: Path) -> None:
    wall = FakeWallClock(at(9, 10))
    source = HistorySource(prior={MONDAY: prior_day(MONDAY)})
    s = session(fast_cfg(tmp_path), wall, FakeConnections(wall, source))
    bot = await s.start()
    try:
        assert next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy)).ema.ready
        assert source.requests == []  # nothing to backfill yet
    finally:
        await s.shutdown()


async def test_a_futures_roll_is_reseeded_by_the_daily_job() -> None:
    sim, source, strat = await _seed_sim({MONDAY: prior_day(MONDAY)})
    strat.set_signal_instrument("NSE_FO|NEXT")  # the roll restarts the EMA
    assert strat.prior_session_bars() == 20
    source.prior = {MONDAY: [c.model_copy(update={"instrument_key": "NSE_FO|NEXT"}) for c in prior_day(MONDAY)]}
    await HistoryLoader(sim.bot, sleep=no_sleep).seed_prior_session()
    assert strat.ema.ready


# --------------------------------------------------------------------------- #
# 2. Close order: no double count in daily P&L
# --------------------------------------------------------------------------- #


async def test_closing_a_trade_does_not_double_count_the_loss() -> None:
    h = Harness()
    await h.enter(ltp=100.0)  # stop 75
    h.tick(CE_KEY, 110.0)  # peak unrealized about +750
    h.tick(CE_KEY, 76.0)
    await h.settle()
    h.tick(CE_KEY, 74.0)  # stop hit
    await h.settle()
    day = h.s.guard.state["orb_options"]
    assert day.unrealized == 0.0 and day.realized < 0
    assert day.max_drawdown == pytest.approx(day.peak_pnl - day.realized, abs=0.01)  # not realized + open loss again


def test_a_close_near_the_limit_does_not_falsely_breach() -> None:
    h = Harness()
    guard = h.s.guard
    guard.record_trade_close("orb_options", -2600.0)
    guard.update_unrealized("orb_options", -1200.0)
    guard.record_trade_close("orb_options", -1250.0, unrealized_after=0.0)  # real loss 3,850; limit 5,000
    day = guard.state["orb_options"]
    assert day.total == pytest.approx(-3850.0) and not day.breached


# --------------------------------------------------------------------------- #
# 3. Stricter warm-up
# --------------------------------------------------------------------------- #


def _tick(ts: datetime, ltp: float = 100.0) -> Tick:
    return Tick(instrument_key=FUT, ltp=ltp, exchange_time=ts, received_at=ts)


def test_a_candle_started_mid_bucket_is_partial() -> None:
    got: list[Candle] = []
    b = CandleBuilder(timeframes=(5,), partial_tolerance_seconds=5.0)
    b.add_listener(got.append)
    b.on_tick(_tick(at(12, 41, 12)))
    b.on_tick(_tick(at(12, 45, 0)))  # contiguous and on time: full
    b.on_tick(_tick(at(12, 50, 1)))
    assert [c.complete for c in got] == [False, True]


def test_the_first_candle_of_a_bot_started_on_time_is_full() -> None:
    got: list[Candle] = []
    b = CandleBuilder(timeframes=(5,), partial_tolerance_seconds=5.0)
    b.add_listener(got.append)
    b.on_tick(_tick(at(9, 15, 2)))
    b.flush(at(9, 20, 5))
    assert [c.complete for c in got] == [True]


def test_a_late_first_tick_after_a_contiguous_candle_is_still_full() -> None:
    got: list[Candle] = []
    b = CandleBuilder(timeframes=(1,), partial_tolerance_seconds=5.0)
    b.add_listener(got.append)
    b.on_tick(_tick(at(10, 0, 0)))
    b.flush(at(10, 1, 3))  # closed by the timer, the path the live bot normally takes
    b.on_tick(_tick(at(10, 1, 40)))  # a quiet instrument: first trade 40 s in
    b.flush(at(10, 2, 3))
    assert [c.complete for c in got] == [True, True]


def test_a_candle_after_a_feed_gap_is_partial() -> None:
    got: list[Candle] = []
    b = CandleBuilder(timeframes=(1,), partial_tolerance_seconds=5.0)
    b.add_listener(got.append)
    b.on_tick(_tick(at(10, 0, 0)))
    b.on_tick(_tick(at(10, 5, 30)))  # bucket 10:05 follows nothing, 30 s late
    b.on_tick(_tick(at(10, 6, 0)))
    assert [c.complete for c in got] == [True, False]


def test_without_a_tolerance_the_builder_marks_nothing_partial() -> None:
    got: list[Candle] = []
    b = CandleBuilder(timeframes=(5,))
    b.add_listener(got.append)
    b.on_tick(_tick(at(12, 41, 12)))
    b.on_tick(_tick(at(12, 45, 0)))
    assert [c.complete for c in got] == [True]


def test_partial_candles_do_not_count_toward_warm_up() -> None:
    s = vwap_strategy()
    partial = five_min(FUT, at(12, 40), [24500.0])[0].model_copy(update={"complete": False})
    assert not s.wants(partial)
    assert s.replay([partial]) == 0 and s.ema.count == 0


def test_a_setup_already_running_when_warm_up_ends_does_not_signal() -> None:
    s = vwap_strategy()  # the 2026-11-09 trade: the 20th candle, bull since the 19th
    signals = [x for c in five_min(FUT, at(9, 15), rising(20), [10_000] * 19 + [60_000]) for x in s.on_candle(c)]
    assert signals == [] and s.ema.ready
    assert not s.armed[OptionType.CE]


def test_a_setup_that_forms_on_the_first_ready_candle_signals() -> None:
    s = vwap_strategy()
    closes = [24500.0] * 18 + [24520.0, 24540.0]
    signals = [x for c in five_min(FUT, at(9, 15), closes, [10_000] * 19 + [30_000]) for x in s.on_candle(c)]
    assert [x.option_type for x in signals] == [OptionType.CE]


def test_a_replayed_setup_must_break_and_form_again() -> None:
    s = vwap_strategy()
    s.replay(five_min(FUT, at(9, 15), rising(30), [10_000] * 30))
    assert s.ema.ready and not s.armed[OptionType.CE]

    follow = at(9, 15) + timedelta(minutes=150)
    closes = [24800.0, 24790.0, 24800.0, 24812.0]  # continues; pulls back; two rising closes again
    candles = five_min(FUT, follow, closes, [30_000, 10_000, 10_000, 40_000])
    assert list(s.on_candle(candles[0])) == [] and s.last_reason == "waiting for a fresh setup"
    assert list(s.on_candle(candles[1])) == [] and s.armed[OptionType.CE]  # the pullback re-arms
    assert list(s.on_candle(candles[2])) == []
    assert [x.option_type for x in s.on_candle(candles[3])] == [OptionType.CE]


def test_replay_skips_candles_already_seen_and_never_signals() -> None:
    s = vwap_strategy()
    candles = five_min(FUT, at(9, 15), rising(30), [10_000] * 30)
    assert s.replay(candles[:25]) == 25
    assert s.replay(candles) == 5 and s.ema.count == 30
    assert s.signals_today == 0


def test_orb_replays_the_range_and_a_past_breakout_is_not_fresh() -> None:
    s = orb_strategy()
    range_candles = opening_range_candles()
    breakout = five_min(INDEX, at(9, 30), [24550.0])
    assert s.replay([*range_candles, *breakout]) == 16 and s.signals_today == 0
    assert s.range_ready and not s.armed[OptionType.CE]

    later = five_min(INDEX, at(9, 35), [24560.0, 24500.0, 24560.0])
    assert list(s.on_candle(later[0])) == [] and s.last_reason == "waiting for a fresh breakout"
    assert list(s.on_candle(later[1])) == []  # close back inside re-arms
    assert [x.option_type for x in s.on_candle(later[2])] == [OptionType.CE]


def test_orb_refuses_an_opening_range_with_missing_minutes() -> None:
    s = orb_strategy()
    candles = opening_range_candles()
    del candles[7]
    s.replay(candles)
    assert s.range_ready and s.range_invalid == "range incomplete: 14 of 15 minutes"
    assert list(s.on_candle(five_min(INDEX, at(9, 30), [24600.0])[0])) == []
    assert "invalid" in (s.last_reason or "")


# --------------------------------------------------------------------------- #
# 4. Conservative stop fills
# --------------------------------------------------------------------------- #


async def test_sl_m_sell_fills_at_the_last_price_when_it_is_below_the_bid() -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL_M, trigger_price=65.2))
    e.quote(65.2, bid=66.65, ask=66.85)  # the 2026-11-09 stop: the quote lagged the print
    assert e.updates[-1].last_fill_price == pytest.approx(65.15)


async def test_sl_m_sell_fills_at_the_bid_when_it_is_below_the_last_price() -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL_M, trigger_price=80.0))
    e.quote(79.9, bid=79.0, ask=80.5)
    assert e.updates[-1].last_fill_price == pytest.approx(78.95)


async def test_sl_m_buy_fills_at_the_higher_of_ask_and_last() -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)
    await e.broker.place_order(req(OrderSide.BUY, otype=OrderType.SL_M, trigger_price=110.0))
    e.quote(112.0, bid=108.0, ask=109.0)
    assert e.updates[-1].last_fill_price == pytest.approx(112.05)


async def test_market_orders_still_fill_at_the_quote() -> None:
    e = Env(slippage_ticks=0)
    e.quote(100.0, bid=101.0, ask=101.5)
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL))
    assert e.updates[-1].last_fill_price == pytest.approx(101.0)  # not the lower last price


async def test_the_stop_trigger_logs_bid_ask_and_last(caplog: pytest.LogCaptureFixture) -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL_M, trigger_price=65.2))
    with caplog.at_level(logging.INFO):
        e.quote(65.2, bid=66.65, ask=66.85)
    rec = next(r for r in caplog.records if r.getMessage() == "paper stop triggered")
    assert (rec.ltp, rec.bid, rec.ask, rec.trigger) == (65.2, 66.65, 66.85, 65.2)  # type: ignore[attr-defined]
    assert rec.tick_time  # type: ignore[attr-defined]


def _no_exchange_stops() -> Any:
    settings = CFG.settings.model_copy(update={"execution": CFG.settings.execution.model_copy(
        update={"use_exchange_stop_orders": False})})
    return CFG.model_copy(update={"settings": settings})


def _lagging_quote(h: Harness, ltp: float, bid: float, ask: float) -> None:
    h.md.on_tick(Tick(instrument_key=CE_KEY, ltp=ltp, bid=bid, ask=ask, bid_qty=1500, ask_qty=1500,
                      exchange_time=h.wall.now, received_at=h.wall.now))


async def test_a_local_monitor_stop_fills_at_the_lower_of_bid_and_last() -> None:
    h = Harness(cfg=_no_exchange_stops(), slippage_ticks=1)
    t = await h.enter(ltp=100.0)
    assert t.stop == pytest.approx(75.0, abs=0.1)
    _lagging_quote(h, ltp=70.0, bid=76.0, ask=76.5)  # the print hit the stop; the quote has not caught up
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.exit_reason == "STOP_LOSS"
    assert rec.exit_price == pytest.approx(69.95)  # not 75.95


async def test_a_local_monitor_stop_uses_the_bid_when_it_is_lower() -> None:
    h = Harness(cfg=_no_exchange_stops(), slippage_ticks=1)
    t = await h.enter(ltp=100.0)
    _lagging_quote(h, ltp=74.0, bid=73.0, ask=74.5)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.exit_price == pytest.approx(72.95)


async def test_exits_that_are_not_stops_still_fill_at_the_quote() -> None:
    h = Harness(slippage_ticks=1)
    t = await h.enter(ltp=100.0)
    _lagging_quote(h, ltp=90.0, bid=92.0, ask=92.5)
    await h.settle()
    assert await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.exit_price == pytest.approx(91.95)


# --------------------------------------------------------------------------- #
# 5. Features at signal time
# --------------------------------------------------------------------------- #


async def test_entry_features_are_captured_before_the_entry_and_hold_nothing_after_it() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    (snap,) = [s for s in sim.repos.feature_snapshots.list_for_trade(trade.trade_uid)
               if s.purpose is SnapshotPurpose.ENTRY]
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.entry_time is not None
    assert snap.snapshot_time <= rec.entry_time
    assert "option_moneyness_pct" in snap.features_json
    assert not [k for k in snap.features_json if k.startswith("trade_")]


async def test_a_feature_failure_never_blocks_the_entry() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    assert sim.bot.learning is not None

    def boom(*_: Any, **__: Any) -> dict[str, float]:
        raise ValueError("no bars")

    sim.bot.learning.features.features = boom  # type: ignore[method-assign]
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    (snap,) = sim.repos.feature_snapshots.list_for_trade(trade.trade_uid)
    assert "error" in snap.features_json


def _journal_row(as_of: datetime | None, feats: dict[str, Any]) -> TradeJournalRecord:
    entry = at(14, 20)
    payload = {**feats, **({"as_of": as_of.isoformat()} if as_of else {})}
    return TradeJournalRecord(
        trade_uid="PAPER-T-vwap_ema-000000000001", mode=TradingMode.PAPER, strategy_id="vwap_ema",
        strategy_version="0.1.0", data_source=EvidenceSource.LIVE_FEED, instrument_key="NSE_FO|1",
        trading_symbol="NIFTY 25000 CE", option_type=OptionType.CE, strike=25000, expiry=DAY, quantity=65,
        entry_time=entry, exit_time=entry + timedelta(minutes=5), entry_price=81.5, exit_price=66.6,
        exit_reason="STOP_LOSS", initial_stop_loss=65.2, final_stop_loss=65.2, target=113.95, gross_pnl=-968.5,
        costs=57.84, net_pnl=-1026.34, slippage=6.5, r_multiple=-0.917, features_entry_json=payload)


def test_learning_ignores_features_captured_after_the_entry() -> None:
    late = _journal_row(at(14, 21), {"option_moneyness_pct": 0.5, "rv_15m": 0.2})
    assert entry_features(late) == {} and moneyness_bucket(late) == "UNKNOWN"
    assert ReflectionEngine().reflect_strategy("vwap_ema", [late]).features == []


def test_learning_keeps_features_captured_by_the_entry() -> None:
    on_time = _journal_row(at(14, 20), {"option_moneyness_pct": 0.5, "rv_15m": 0.2})
    assert entry_features(on_time)["rv_15m"] == 0.2 and moneyness_bucket(on_time) == "ITM"
    assert {f.feature for f in ReflectionEngine().reflect_strategy("vwap_ema", [on_time]).features} == \
        {"option_moneyness_pct", "rv_15m"}


async def test_the_journal_drops_an_entry_snapshot_taken_after_the_entry() -> None:
    h = Harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE_KEY, 74.0)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.entry_time is not None
    late = FeatureSnapshotRecord(mode=TradingMode.PAPER, strategy_id=rec.strategy_id, trade_uid=rec.trade_uid,
                                 instrument_key=CE_KEY, purpose=SnapshotPurpose.ENTRY,
                                 snapshot_time=rec.entry_time + timedelta(minutes=1),
                                 feature_schema_version="journal-1", features_json={"rv_15m": 0.2})
    entry = build_entry(TradeFacts(rec, snapshots=[late]), data_source=EvidenceSource.LIVE_FEED)
    assert entry.features_entry_json == {}
    on_time = late.model_copy(update={"snapshot_time": rec.entry_time})
    entry = build_entry(TradeFacts(rec, snapshots=[on_time]), data_source=EvidenceSource.LIVE_FEED)
    assert entry.features_entry_json["rv_15m"] == 0.2


# --------------------------------------------------------------------------- #
# 6. Recording, decision logs, spread at entry, frozen price
# --------------------------------------------------------------------------- #


def _recorder(repos: Repositories, held: set[str] | None = None) -> MarketRecorder:
    return MarketRecorder(repos.market_history, CFG.settings.market_recording, is_option=lambda k: "CE" in k,
                          held_keys=lambda: held or set())


def _db() -> tuple[Database, Repositories]:
    db = Database(":memory:")
    db.initialize()
    return db, Repositories(db, TradingMode.PAPER)


def test_schema_v6_has_the_recording_tables() -> None:
    db, _ = _db()
    assert SCHEMA_VERSION == 7 and {"candles", "option_quotes"} <= set(db.tables())


def test_candles_are_saved_and_a_full_candle_replaces_a_partial_one() -> None:
    _, repos = _db()
    rec = _recorder(repos)
    full = five_min(FUT, at(12, 40), [24500.0], [9000])[0]
    partial = full.model_copy(update={"complete": False, "volume": 100})

    rec.on_candle(partial)
    assert rec.flush() == 1
    (row,) = repos.market_history.candles(FUT, 5, DAY)
    assert (row["complete"], row["source"], row["volume"]) == (0, "LIVE", 100)

    rec.record_backfill([full])
    rec.flush()
    (row,) = repos.market_history.candles(FUT, 5, DAY)
    assert (row["complete"], row["source"], row["volume"]) == (1, "BACKFILL", 9000)

    rec.on_candle(partial)  # a complete candle is never rewritten
    rec.flush()
    (row,) = repos.market_history.candles(FUT, 5, DAY)
    assert (row["complete"], row["volume"]) == (1, 9000)


def test_option_quotes_are_sampled_and_held_contracts_sampled_faster() -> None:
    _, repos = _db()
    key, held_key = "NSE_FO|24500CE", "NSE_FO|24600CE"
    rec = _recorder(repos, {held_key})
    for s in range(0, 11):
        t = at(10, 0, s)
        rec.on_tick(tick(key, 100.0, t))
        rec.on_tick(tick(held_key, 90.0, t))
    rec.flush()
    assert len(repos.market_history.quotes(key)) == 3  # 0, 5, 10 s
    rows = repos.market_history.quotes(held_key)
    assert len(rows) == 11 and rows[0]["held"] == 1
    assert rows[0]["bid"] is not None and rows[0]["ask"] is not None and rows[0]["ltp"] == 90.0


def test_only_live_option_ticks_are_recorded() -> None:
    _, repos = _db()
    rec = _recorder(repos)
    rec.on_tick(tick(INDEX, 24500.0, at(10, 0)))
    snap = tick("NSE_FO|24500CE", 100.0, at(10, 0)).model_copy(update={"is_snapshot": True})
    rec.on_tick(snap)
    assert rec.flush() == 0


def test_retention_deletes_only_quotes_older_than_the_limit() -> None:
    _, repos = _db()
    key = "NSE_FO|24500CE"
    rec = _recorder(repos)
    repos.market_history.save_quotes([(tick(key, 1.0, at(15, 29, day=(2026, 10, 3))), False),  # 31 days old
                                      (tick(key, 2.0, at(9, 15, day=(2026, 10, 4))), False),  # exactly 30 days
                                      (tick(key, 3.0, at(10, 0)), False)])
    assert rec.purge_old_quotes(DAY) == 1
    assert [r["ltp"] for r in repos.market_history.quotes(key)] == [2.0, 3.0]
    assert rec.purge_old_quotes(DAY) == 0  # repeat-safe


def test_retention_leaves_candles_alone() -> None:
    _, repos = _db()
    rec = _recorder(repos)
    old = five_min(FUT, at(9, 15, day=(2026, 1, 5)), [24500.0])[0]
    rec.record_backfill([old])
    rec.flush()
    rec.purge_old_quotes(DAY)
    assert len(repos.market_history.candles(FUT, 5, date(2026, 1, 5))) == 1


def test_a_retention_failure_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    db, repos = _db()
    rec = _recorder(repos)
    db.execute("DROP TABLE option_quotes")
    with caplog.at_level(logging.ERROR):
        assert rec.purge_old_quotes(DAY) == 0
    assert rec.failures == 1


@pytest.mark.parametrize("days", [0, 366])
def test_the_retention_setting_is_bounded(days: int) -> None:
    with pytest.raises(ValueError):
        MarketRecordingSection(option_quote_retention_days=days)


async def test_a_session_applies_retention_at_start_and_schedules_it(tmp_path: Path) -> None:
    cfg = fast_cfg(tmp_path)
    db = Database(cfg.database_path)
    db.initialize()
    key = "NSE_FO|24500CE"
    old = tick(key, 1.0, at(10, 0, day=(2026, 1, 5)))
    Repositories(db, TradingMode.PAPER).market_history.save_quotes([(old, False)])
    db.close()
    wall = FakeWallClock(at(9, 14))
    s = session(cfg, wall, FakeConnections(wall))
    bot = await s.start()
    try:
        assert bot.repos.market_history.quotes(key) == []
        assert {"option_quote_retention", "prior_session_seed"} <= set(bot.scheduler.jobs)
    finally:
        await s.shutdown()


def test_a_recording_failure_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    db, repos = _db()
    rec = _recorder(repos)
    rec.on_candle(five_min(FUT, at(12, 40), [24500.0])[0])
    db.execute("DROP TABLE candles")
    with caplog.at_level(logging.ERROR):
        assert rec.flush() == 0
    assert rec.failures == 1


async def test_a_session_saves_candles_quotes_and_logs_every_decision(tmp_path: Path,
                                                                      caplog: pytest.LogCaptureFixture) -> None:
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    cfg = paper_cfg(tmp_path)
    s = session(cfg, wall, conns)
    bot = await s.start()
    with caplog.at_level(logging.INFO):
        await drive(s, conns, ORB_DAY)
    assert s.market_recorder is not None
    s.market_recorder.flush()
    candles = bot.repos.market_history.candles(INDEX, 5, DAY)
    assert candles and all(r["source"] == "LIVE" for r in candles)
    (held,) = bot.stack.positions.open_trades("orb_options")
    assert any(r["held"] == 1 for r in bot.repos.market_history.quotes(held.instrument.instrument_key))

    decisions = [r for r in caplog.records if r.getMessage() == "strategy candle evaluated"]
    orb = [r for r in decisions if r.strategy_id == "orb_options"]  # type: ignore[attr-defined]
    assert orb and all(r.reason and r.candle_end for r in orb)  # type: ignore[attr-defined]
    assert any(r.signal for r in orb)  # type: ignore[attr-defined]
    await s.shutdown()


def test_decision_logs_are_off_unless_the_session_turns_them_on(caplog: pytest.LogCaptureFixture) -> None:
    s = vwap_strategy()  # backtests build strategies this way: no per-candle log lines
    with caplog.at_level(logging.INFO):
        s.on_candle(five_min(FUT, at(9, 15), [24500.0], [100])[0])
    assert not [r for r in caplog.records if r.getMessage() == "strategy candle evaluated"]


def test_the_no_signal_log_explains_the_candle(caplog: pytest.LogCaptureFixture) -> None:
    s = vwap_strategy()
    s.log_decisions = True
    with caplog.at_level(logging.INFO):
        s.on_candle(five_min(FUT, at(9, 15), [24500.0], [100])[0])
    rec = next(r for r in caplog.records if r.getMessage() == "strategy candle evaluated")
    assert rec.reason == "warming up" and rec.ema_bars == 1 and rec.signal is False  # type: ignore[attr-defined]
    assert rec.vwap is not None and rec.candle_end == at(9, 20).isoformat()  # type: ignore[attr-defined]


async def test_spread_is_logged_when_an_entry_is_approved(caplog: pytest.LogCaptureFixture) -> None:
    h = Harness()
    with caplog.at_level(logging.INFO):
        await h.enter(ltp=100.0, spread=0.5)
    rec = next(r for r in caplog.records if r.getMessage() == "entry approved by risk")
    assert rec.bid == 99.75 and rec.ask == 100.25 and rec.ltp == 100.0  # type: ignore[attr-defined]
    assert rec.spread == 0.5 and rec.spread_pct == 0.5  # type: ignore[attr-defined]


def test_frozen_seconds_tracks_how_long_a_ticking_price_has_not_moved() -> None:
    wall = FakeWallClock(at(10, 0))
    md = MarketDataService(now_fn=wall)
    assert md.frozen_seconds(INDEX) is None
    for s in (0, 5):
        wall.now = at(10, 0, s)
        md.on_tick(tick(INDEX, 24500.0, wall.now))
    assert md.frozen_seconds(INDEX) == 5.0
    wall.now = at(10, 0, 8)
    md.on_tick(tick(INDEX, 24500.5, wall.now))
    assert md.frozen_seconds(INDEX) == 0.0


def test_a_silent_feed_is_not_reported_as_frozen() -> None:
    wall = FakeWallClock(at(10, 0))
    md = MarketDataService(stale_after_seconds=10, now_fn=wall)
    md.on_tick(tick(INDEX, 24500.0, wall.now))
    wall.advance(seconds=30)
    assert md.frozen_seconds(INDEX) is None  # staleness, not a frozen price


def test_snapshots_do_not_count_as_price_updates() -> None:
    wall = FakeWallClock(at(10, 0))
    md = MarketDataService(now_fn=wall)
    md.on_tick(tick(INDEX, 24500.0, wall.now).model_copy(update={"is_snapshot": True}))
    assert md.frozen_seconds(INDEX) is None


async def _frozen_sim(until_second: int, start: tuple[int, int] = (10, 0)) -> Sim:
    sim = Sim(at(*start))
    await sim.bot.prepare()
    t0 = at(*start)
    for s in range(0, until_second + 1, 5):
        sim.wall.now = t0 + timedelta(seconds=s)
        sim.md.on_tick(tick(INDEX, 24500.0, sim.wall.now, spread=0.0))
    return sim


async def test_a_frozen_nifty_price_blocks_new_entries_until_it_moves(caplog: pytest.LogCaptureFixture) -> None:
    assert (await _frozen_sim(55)).bot.entry_block_reason() is None  # within 60 s
    sim = await _frozen_sim(65)
    with caplog.at_level(logging.WARNING):
        reason = sim.bot.entry_block_reason()
        assert sim.bot.entry_block_reason() == reason  # logged once per episode
    assert reason is not None and "price unchanged" in reason and "exits unaffected" in reason
    assert len([r for r in caplog.records if "NIFTY price frozen" in r.getMessage()]) == 1

    sim.wall.advance(seconds=1)
    sim.md.on_tick(tick(INDEX, 24501.0, sim.wall.now, spread=0.0))
    assert sim.bot.entry_block_reason() is None


async def test_a_frozen_price_outside_market_hours_blocks_nothing() -> None:
    sim = await _frozen_sim(65, start=(15, 30))
    assert sim.bot.frozen_price_block_reason() is None


async def test_a_frozen_price_never_stops_the_stop_loss() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    t0 = at(9, 50)
    for s in range(5, 80, 5):
        await sim.step(t0 + timedelta(seconds=s), 24565.0, premium=100.0)
    assert sim.bot.entry_block_reason() is not None
    await sim.step(t0 + timedelta(seconds=85), 24565.0, premium=70.0)
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"


def test_the_feed_summary_reports_the_frozen_time(caplog: pytest.LogCaptureFixture) -> None:
    wall = FakeWallClock(at(10, 0))
    md = MarketDataService(now_fn=wall)
    md.on_tick(tick(INDEX, 24500.0, wall.now))
    wall.advance(seconds=4)
    md.on_tick(tick(INDEX, 24500.0, wall.now))
    monitor = FeedHealthMonitor(FakeFeed(), md, reference_key=INDEX, max_lag_seconds=2.0)
    assert monitor.check()["frozen_price_seconds"] == 4.0
