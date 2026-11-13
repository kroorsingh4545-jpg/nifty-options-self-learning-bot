"""PAPER day-2 fix: a start-up inside a minute must not leave a hole all day.

On 2026-11-12 the bot started at 09:16:57. The 09:16 minute was partial, its
repair ran out of attempts at 09:17:08 (Upstox had not published it yet), and
ORB kept "range incomplete: 14 of 15 minutes" for the rest of the day. Missed
candles now stay pending and are fetched again each time a later candle of the
series closes; a strategy that already moved past the hole is rebuilt from its
start-of-day state.

All market data here is SYNTHETIC.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from core.enums import OptionType
from core.models import Candle
from ml.feature_engineering import FeatureEngine, SessionTimes
from runtime.history import MAX_FILL_ATTEMPTS, REPAIR_ATTEMPTS, HistoryLoader
from strategies.orb_options import OrbOptionsStrategy
from strategies.vwap_ema import VwapEmaStrategy
from tests.fakes import FakeWallClock
from tests.paper_support import FakeConnections, session
from tests.synthetic import FUT, INDEX, at, candle, five_min, opening_range_candles, tick
from tests.unit.test_bot_integration import Sim, no_sleep
from tests.unit.test_paper_day1_fixes import (
    BARS_5M,
    HistorySource,
    fast_cfg,
    morning,
    paused,
    prior_day,
    rising,
    vwap_strategy,
)

RANGE = opening_range_candles(INDEX, high=24520, low=24480)  # 09:15 .. 09:29, extremes at 09:19 and 09:24
FUT_0915 = candle(FUT, 1, at(9, 15), 24600, 24601, 24599, 24600, 650)  # the features also read the future
FULL_RANGE = {"high": 24520, "low": 24480, "ready": True, "invalid": None, "minutes": 15}


def orb_of(bot: object) -> OrbOptionsStrategy:
    return next(x for x in bot.strategies if isinstance(x, OrbOptionsStrategy))  # type: ignore[attr-defined]


def vwap_of(bot: object) -> VwapEmaStrategy:
    return next(x for x in bot.strategies if isinstance(x, VwapEmaStrategy))  # type: ignore[attr-defined]


def partial(c: Candle) -> Candle:
    return c.model_copy(update={"complete": False})


async def _mid_minute_start() -> tuple[Sim, HistorySource, HistoryLoader]:
    """The 2026-11-12 start at 09:16:57: Upstox has only the 09:15 minute; 09:16 is forming."""
    sim = Sim(at(9, 16, 57))
    await sim.bot.prepare()
    source = HistorySource({(INDEX, 1): RANGE[:1], (FUT, 1): [FUT_0915]})
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.seed_prior_session()
    await loader.backfill_today()
    return sim, source, loader


def feed(sim: Sim, loader: HistoryLoader, c: Candle) -> None:
    """A live candle, in the session's listener order: the bot's own listeners
    (engine, features) first, then history (attached last, as in PaperSession)."""
    sim.wall.now = c.end
    sim.bot.candles._publish([c])
    loader.on_candle(c)


async def settle(loader: HistoryLoader) -> None:
    while loader._tasks:
        await asyncio.gather(*list(loader._tasks), return_exceptions=True)


# --------------------------------------------------------------------------- #
# The 2026-11-12 case
# --------------------------------------------------------------------------- #

async def test_a_start_inside_a_range_minute_still_gives_orb_all_15_minutes(
        caplog: pytest.LogCaptureFixture) -> None:
    sim, source, loader = await _mid_minute_start()
    orb = orb_of(sim.bot)
    assert orb.or_minutes == 1

    # 09:17: the bot's own 09:16 candle is partial and Upstox has not published it yet
    sim.wall.now = at(9, 17, 0)
    with caplog.at_level(logging.WARNING):
        loader.on_candle(partial(RANGE[1]))
        await settle(loader)
    assert any(r.getMessage() == "partial candle could not be repaired" for r in caplog.records)
    assert loader._pending == {(INDEX, 1): {at(9, 16)}}

    # 09:18: the next minute closes; by now Upstox has 09:16
    source.data[(INDEX, 1)] = RANGE[:3]
    feed(sim, loader, RANGE[2])
    await settle(loader)
    assert loader._pending == {}
    assert orb.or_minutes == 3 and orb.seen_until(1) == at(9, 18)

    for c in RANGE[3:]:
        feed(sim, loader, c)
    for c in five_min(INDEX, at(9, 15), [24500.0] * 3):
        feed(sim, loader, c)
    assert orb.snapshot()["opening_range"] == FULL_RANGE  # was "range incomplete: 14 of 15 minutes"
    assert paused(sim.bot, "orb_options") is None
    assert orb.signals_today == 0


async def test_a_minute_still_missing_is_fetched_again_at_each_close_until_it_arrives() -> None:
    sim, source, loader = await _mid_minute_start()
    sim.wall.now = at(9, 17, 0)
    loader.on_candle(partial(RANGE[1]))
    await settle(loader)

    source.requests.clear()
    feed(sim, loader, RANGE[2])  # 09:18: still not published
    await settle(loader)
    assert source.requests == [(INDEX, 1)] and loader._pending == {(INDEX, 1): {at(9, 16)}}

    source.data[(INDEX, 1)] = RANGE[:4]
    feed(sim, loader, RANGE[3])  # 09:19: now it is
    await settle(loader)
    assert source.requests == [(INDEX, 1)] * 2 and loader._pending == {}
    assert orb_of(sim.bot).or_minutes == 4

    feed(sim, loader, RANGE[4])  # nothing pending: no more fetches
    await settle(loader)
    assert len(source.requests) == 2


async def test_a_newest_candle_not_published_at_start_up_is_filled_at_the_next_close() -> None:
    sim = Sim(at(9, 17, 2))  # the 09:16 minute has just ended but Upstox does not have it yet
    await sim.bot.prepare()
    source = HistorySource({(INDEX, 1): RANGE[:1], (FUT, 1): [FUT_0915]})
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.backfill_today()
    assert loader._pending == {(INDEX, 1): {at(9, 16)}, (FUT, 1): {at(9, 16)}}
    assert source.requests.count((INDEX, 1)) == REPAIR_ATTEMPTS

    source.data[(INDEX, 1)] = RANGE[:3]
    sim.wall.now = at(9, 18, 0)
    loader.on_candle(partial(RANGE[2]))  # the bot's own 09:17 candle is partial; its repair fetches 09:16 too
    await settle(loader)
    assert (INDEX, 1) not in loader._pending and orb_of(sim.bot).or_minutes == 3


async def test_a_start_up_inside_the_range_with_a_real_session_end_to_end(tmp_path: Path) -> None:
    """Through PaperSession and the real CandleBuilder: ticks, not hand-made candles."""
    wall = FakeWallClock(at(9, 16, 57))
    source = HistorySource({(INDEX, 1): RANGE[:1], (FUT, 1): [FUT_0915]})
    conns = FakeConnections(wall, source)
    s = session(fast_cfg(tmp_path), wall, conns)
    bot = await s.start()
    try:
        assert conns.md is not None and s.history is not None
        orb = orb_of(bot)
        conns.md.on_tick(tick(INDEX, 24500.0, at(9, 16, 58), spread=0.0))  # inside the 09:16 minute
        wall.now = at(9, 17, 0)
        conns.md.on_tick(tick(INDEX, 24500.0, wall.now, spread=0.0))  # closes the partial 09:16 candle
        await settle(s.history)
        assert s.history._pending == {(INDEX, 1): {at(9, 16)}}

        source.data[(INDEX, 1)] = RANGE  # Upstox catches up
        source.data[(INDEX, 5)] = five_min(INDEX, at(9, 15), [24500.0] * 3)
        for minute in range(18, 31):
            wall.now = at(9, minute, 0)
            price = 24500.0 if minute < 30 else 24501.0
            conns.md.on_tick(tick(INDEX, price, wall.now, spread=0.0))
            await settle(s.history)
        assert orb.or_minutes == 15 and orb.range_ready
        assert orb.range_invalid is None
        assert paused(bot, "orb_options") is None
    finally:
        await s.shutdown()


# --------------------------------------------------------------------------- #
# Gaps later in the day
# --------------------------------------------------------------------------- #

async def test_a_feed_gap_is_filled_but_the_block_for_the_day_stays(caplog: pytest.LogCaptureFixture) -> None:
    sim = Sim(at(12, 41))
    await sim.bot.prepare()
    source = HistorySource(morning())
    sim.bot.source = source
    loader = HistoryLoader(sim.bot, sleep=no_sleep)
    await loader.seed_prior_session()
    await loader.backfill_today()
    vw = vwap_of(sim.bot)

    bars = five_min(FUT, at(12, 40), [24900.0, 24910.0, 24920.0], [8000] * 3)
    source.data[(FUT, 5)] = [*source.data[(FUT, 5)], *bars]
    with caplog.at_level(logging.WARNING):
        feed(sim, loader, bars[1])  # 12:40 never came over the feed
        await settle(loader)
    assert "candle gap 12:40-12:45" in (paused(sim.bot, "vwap_ema") or "")
    assert any(r.getMessage() == "missed candles filled" for r in caplog.records)
    assert any(r.getMessage() == "strategy rebuilt with the late candles" for r in caplog.records)

    reference = vwap_strategy()
    for c in [*morning()[(FUT, 5)], *bars[:2]]:
        reference.on_candle(c)
    assert vw.ema.count == BARS_5M + 2 and vw.ema.value == pytest.approx(reference.ema.value)
    assert vw.vwap.value == pytest.approx(reference.vwap.value)
    assert [c.start for c in vw.recent][-2:] == [at(12, 40), at(12, 45)]
    assert "candle gap" in (paused(sim.bot, "vwap_ema") or "")  # the fill never lifts the block


async def test_a_candle_that_never_arrives_is_given_up_after_the_limit(caplog: pytest.LogCaptureFixture) -> None:
    sim, source, loader = await _mid_minute_start()
    sim.wall.now = at(9, 17, 0)
    loader.on_candle(partial(RANGE[1]))
    await settle(loader)
    source.requests.clear()
    source.data[(INDEX, 1)] = [RANGE[0], *RANGE[2:]]  # Upstox never gets 09:16
    with caplog.at_level(logging.ERROR):
        for i in range(MAX_FILL_ATTEMPTS + 5):
            feed(sim, loader, candle(INDEX, 1, at(9, 17) + timedelta(minutes=i), 24500, 24501, 24499, 24500))
            await settle(loader)
    assert len(source.requests) == MAX_FILL_ATTEMPTS and loader._pending == {}
    assert any(r.getMessage() == "missed candles could not be filled" for r in caplog.records)
    assert "range incomplete: 14 of 15 minutes" in (orb_of(sim.bot).range_invalid or "")


async def test_a_failed_fetch_while_filling_is_retried_at_the_next_close() -> None:
    sim, source, loader = await _mid_minute_start()
    sim.wall.now = at(9, 17, 0)
    loader.on_candle(partial(RANGE[1]))
    await settle(loader)
    source.fail = True
    feed(sim, loader, RANGE[2])
    await settle(loader)
    assert loader._pending == {(INDEX, 1): {at(9, 16)}}
    source.fail = False
    source.data[(INDEX, 1)] = RANGE[:4]
    feed(sim, loader, RANGE[3])
    await settle(loader)
    assert loader._pending == {} and orb_of(sim.bot).or_minutes == 4


async def test_the_late_candle_is_recorded_and_put_in_place_in_the_features() -> None:
    sim = Sim(at(9, 16, 57))
    await sim.bot.prepare()
    source = HistorySource({(INDEX, 1): RANGE[:1], (FUT, 1): [FUT_0915]})
    sim.bot.source = source
    recorded: list[Candle] = []
    loader = HistoryLoader(sim.bot, sleep=no_sleep, on_candles=recorded.extend)
    await loader.backfill_today()
    sim.wall.now = at(9, 17, 0)
    loader.on_candle(partial(RANGE[1]))
    await settle(loader)

    source.data[(INDEX, 1)] = RANGE[:3]
    feed(sim, loader, RANGE[2])
    await settle(loader)
    assert at(9, 16) in [c.start for c in recorded if c.instrument_key == INDEX]
    assert sim.bot.learning is not None
    engine = sim.bot.learning.features
    starts = [c.start for c in engine._index.candles]
    assert starts == sorted(starts) and at(9, 16) in starts and engine.last_bar_end == at(9, 18)


# --------------------------------------------------------------------------- #
# Rebuild: what must survive it
# --------------------------------------------------------------------------- #

def test_a_rebuild_keeps_the_seeded_ema_and_todays_entries() -> None:
    day = date(2026, 11, 3)
    s = vwap_strategy()
    s.seed_prior_session(prior_day(date(2026, 11, 2)))
    saved = s.checkpoint()
    bars = five_min(FUT, at(9, 15), rising(10), [1000] * 10)
    s.replay([*bars[:4], *bars[5:]])  # 09:35 missing
    s.restore_entries(day, {OptionType.CE: 1, OptionType.PE: 0})
    s.signals_today = 2

    used = s.rebuild(saved, bars)
    reference = vwap_strategy()
    reference.seed_prior_session(prior_day(date(2026, 11, 2)))
    reference.replay(bars)
    assert used == 10 and s.ema.count == reference.ema.count == 85
    assert s.ema.value == pytest.approx(reference.ema.value) and s.vwap.value == pytest.approx(reference.vwap.value)
    assert s.entries[OptionType.CE] == 1 and s.signals_today == 2 and not s.replaying


def test_a_failed_rebuild_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    s = vwap_strategy()
    saved = s.checkpoint()
    bars = five_min(FUT, at(9, 15), rising(6), [1000] * 6)
    s.replay(bars[:3])
    before = (s.ema.count, s.ema.value, s.vwap.value, dict(s._last_end))
    real = VwapEmaStrategy.generate

    def flaky(self: VwapEmaStrategy, c: Candle) -> object:
        if c.start == at(9, 25):
            raise ValueError("bad candle")
        return real(self, c)

    monkeypatch.setattr(VwapEmaStrategy, "generate", flaky)
    with pytest.raises(ValueError, match="bad candle"):
        s.rebuild(saved, bars)
    assert (s.ema.count, s.ema.value, s.vwap.value, dict(s._last_end)) == before


async def test_a_rebuild_never_signals_or_trades() -> None:
    sim, source, loader = await _mid_minute_start()
    assert sim.bot.engine is not None
    sim.wall.now = at(9, 17, 0)
    loader.on_candle(partial(RANGE[1]))
    await settle(loader)
    source.data[(INDEX, 1)] = RANGE[:3]
    feed(sim, loader, RANGE[2])
    await settle(loader)
    assert sim.bot.engine.state.strategy("orb_options").signals == 0
    assert sim.bot.stack.positions.open_trade_count() == 0


def test_feature_series_insert_keeps_order_and_ignores_duplicates() -> None:
    engine = FeatureEngine(index_key=INDEX, times=SessionTimes())
    bars = [candle(INDEX, 1, at(9, 15) + timedelta(minutes=i), 24500 + i, 24501 + i, 24499 + i, 24500 + i)
            for i in range(4)]
    for c in (bars[0], bars[2], bars[3]):
        engine.on_candle(c)
    engine.fill_candle(bars[1])
    engine.fill_candle(bars[1])
    engine.on_candle(bars[1])  # out of order through the live path: still ignored
    assert [c.start for c in engine._index.candles] == [c.start for c in bars]
    engine.fill_candle(candle(INDEX, 1, at(9, 15, day=(2026, 11, 2)), 1, 1, 1, 1))  # an older day is refused
    assert len(engine._index.candles) == 4
