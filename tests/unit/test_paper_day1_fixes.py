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
from core.enums import EvidenceSource, OptionType, OrderSide, OrderType, SnapshotPurpose, TradeStatus, TradingMode
from core.models import Candle, Tick
from data.candle_builder import CandleBuilder
from data.market_data import MarketDataService
from data.storage.database import SCHEMA_VERSION, Database
from data.storage.records import FeatureSnapshotRecord, TradeJournalRecord
from data.storage.repositories import Repositories
from learning.reflection_engine import ReflectionEngine, entry_features, moneyness_bucket
from learning.trade_journal import TradeFacts, build_entry
from runtime.history import REPAIR_ATTEMPTS, HistoryLoader
from runtime.market_recorder import MarketRecorder
from runtime.paper import FeedHealthMonitor
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
    """The Upstox candle API, serving SYNTHETIC candles."""

    def __init__(self, data: dict[tuple[str, int], list[Candle]] | None = None) -> None:
        super().__init__()
        self.data = data or {}
        self.fail = False
        self.requests: list[tuple[str, int]] = []

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        self.requests.append((instrument_key, interval_minutes))
        if self.fail:
            raise ConnectionError("candle API down")
        return self.data.get((instrument_key, interval_minutes), [])


# --------------------------------------------------------------------------- #
# 1. Backfill at start-up
# --------------------------------------------------------------------------- #


def morning(volumes: int = 10_000) -> dict[tuple[str, int], list[Candle]]:
    closes = rising(40)  # 09:15 to 12:35: every candle complete by 12:41
    return {(FUT, 5): five_min(FUT, at(9, 15), closes, [volumes] * 40),
            (INDEX, 1): opening_range_candles(INDEX, high=24520, low=24480),
            (INDEX, 5): five_min(INDEX, at(9, 15), [24500.0] * 40),
            (FUT, 1): [candle(FUT, 1, at(9, 15) + timedelta(minutes=i), 24500, 24501, 24499, 24500, 65)
                       for i in range(15)]}


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
        assert got.ema.count == 40 and got.ema.ready and got.ema.value == pytest.approx(reference.ema.value)
        assert got.vwap.value == pytest.approx(reference.vwap.value)  # a true session VWAP, from 09:15
        assert got.volume.mean == reference.volume.mean and list(got.sides) == list(reference.sides)

        assert orb.snapshot()["opening_range"] == {"high": 24520, "low": 24480, "ready": True, "invalid": None,
                                                   "minutes": 15}
        assert bot.learning is not None and bot.learning.features.last_bar_end == at(9, 30)

        assert bot.engine is not None
        assert bot.engine.state.strategy("vwap_ema").signals == 0  # replay cannot signal
        assert got.signals_today == 0 and orb.signals_today == 0
        assert set(source.requests) == {(FUT, 5), (INDEX, 1), (INDEX, 5), (FUT, 1)}
    finally:
        await s.shutdown()


async def test_backfill_ignores_forming_other_day_and_misaligned_candles() -> None:
    sim = Sim(at(12, 41))
    await sim.bot.prepare()
    good = five_min(FUT, at(9, 15), [24500.0])[0]
    forming = five_min(FUT, at(12, 40), [24500.0])[0].model_copy(update={"complete": False})
    not_ended = five_min(FUT, at(12, 40), [24500.0])[0]  # marked complete, but ends after now
    yesterday = five_min(FUT, at(9, 15, day=(2026, 11, 2)), [24500.0])[0]
    misaligned = five_min(FUT, at(9, 17), [24500.0])[0]
    sim.bot.source = HistorySource({(FUT, 5): [yesterday, good, misaligned, forming, not_ended]})
    await HistoryLoader(sim.bot, sleep=no_sleep).backfill_today()
    strat = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    assert strat.ema.count == 1


async def test_a_failed_backfill_never_stops_the_session(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    wall = FakeWallClock(at(12, 41))
    source = HistorySource()
    source.fail = True
    s = session(paper_cfg(tmp_path), wall, FakeConnections(wall, source))
    with caplog.at_level(logging.WARNING):
        bot = await s.start()
    try:
        assert any(r.getMessage().startswith("candle backfill failed") for r in caplog.records)
        strat = next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy))
        assert strat.ema.count == 0  # still warming up from live candles
        assert bot.engine is not None and bot.engine.accepting_signals
    finally:
        await s.shutdown()


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
    source = HistorySource({(FUT, 5): five_min(FUT, at(9, 15), rising(40))})
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.backfill_today()
    strat = next(x for x in sim.bot.strategies if isinstance(x, VwapEmaStrategy))
    return sim, source, loader, strat


async def test_a_partial_candle_is_repaired_from_upstox_once() -> None:
    sim, source, loader, strat = await _prepared_loader(at(12, 41))
    assert strat.ema.count == 40
    full = five_min(FUT, at(12, 35), [24900.0, 24910.0])  # 12:35 and 12:40 buckets
    source.data[(FUT, 5)] = [*source.data[(FUT, 5)], *full]
    sim.wall.now = at(12, 45, 3)
    partial = full[1].model_copy(update={"complete": False})

    loader.on_candle(partial)
    await asyncio.gather(*loader._tasks)
    assert strat.ema.count == 42  # the skipped 12:35 bar and the repaired 12:40 bar
    loader.on_candle(partial)  # a repeat cannot count a candle twice
    await asyncio.gather(*loader._tasks)
    assert strat.ema.count == 42


async def test_an_unrepairable_partial_candle_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    sim, source, loader, strat = await _prepared_loader(at(12, 41))
    source.requests.clear()
    sim.wall.now = at(12, 45, 3)
    partial = five_min(FUT, at(12, 40), [24900.0])[0].model_copy(update={"complete": False})
    with caplog.at_level(logging.WARNING):
        loader.on_candle(partial)
        await asyncio.gather(*loader._tasks)
    assert len(source.requests) == REPAIR_ATTEMPTS and strat.ema.count == 40
    assert any("could not be repaired" in r.getMessage() for r in caplog.records)


async def test_a_gap_after_the_backfill_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    sim, _, loader, _ = await _prepared_loader(at(12, 41))
    sim.wall.now = at(12, 50)
    with caplog.at_level(logging.WARNING):
        loader.on_candle(five_min(FUT, at(12, 45), [24900.0])[0])  # backfill ended at 12:35
    assert any("gap between the backfill" in r.getMessage() for r in caplog.records)


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
    assert SCHEMA_VERSION == 6 and {"candles", "option_quotes"} <= set(db.tables())


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
