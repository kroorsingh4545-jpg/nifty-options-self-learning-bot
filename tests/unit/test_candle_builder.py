"""Candle building from synthetic ticks."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.clock import IST
from core.models import Candle, Tick
from data.candle_builder import CandleBuilder
from tests.fakes import T0

K = "NSE_INDEX|Nifty 50"


def tick(at: datetime, ltp: float, vtt: int | None = None, key: str = K) -> Tick:
    return Tick(instrument_key=key, ltp=ltp, exchange_time=at, volume_today=vtt)


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 3, h, m, s, tzinfo=IST)


def test_buckets_align_to_session_open() -> None:
    b = CandleBuilder()
    assert b.bucket(at(9, 17, 59), 3) == (at(9, 15), at(9, 18))
    assert b.bucket(at(9, 18), 3) == (at(9, 18), at(9, 21))
    assert b.bucket(at(9, 29, 59), 15) == (at(9, 15), at(9, 30))
    assert b.bucket(at(15, 29), 15) == (at(15, 15), at(15, 30))
    assert b.bucket(at(10, 4), 5) == (at(10, 0), at(10, 5))


def test_one_minute_ohlcv() -> None:
    b = CandleBuilder(timeframes=[1])
    for s, px, v in [(0, 100, 1000), (10, 105, 1300), (20, 98, 1350), (59, 101, 1500)]:
        assert b.on_tick(tick(T0 + timedelta(seconds=s), px, v)) == []
    (c,) = b.on_tick(tick(at(9, 16, 1), 102, 1600))
    assert (c.open, c.high, c.low, c.close) == (100, 105, 98, 101)
    assert c.volume == 500  # first tick sets baseline: 1500 - 1000
    assert c.tick_count == 4 and c.complete
    assert (c.start, c.end) == (at(9, 15), at(9, 16))


def test_all_timeframes_complete_at_right_times() -> None:
    b = CandleBuilder()
    got: list[Candle] = []
    b.add_listener(got.append)
    t, px = T0, 24500.0
    while t < at(9, 45):
        b.on_tick(tick(t, px))
        t += timedelta(seconds=20)
        px += 1
    by_tf: dict[int, list[Candle]] = {}
    for c in got:
        by_tf.setdefault(c.timeframe_minutes, []).append(c)
    assert len(by_tf[1]) == 29  # 09:15..09:43 closed; 09:44 still forming
    assert len(by_tf[3]) == 9
    assert len(by_tf[5]) == 5
    assert len(by_tf[15]) == 1
    orb = by_tf[15][0]
    assert (orb.start, orb.end) == (at(9, 15), at(9, 30))
    assert orb.open == 24500 and orb.close == 24500 + 44


def test_15m_range_matches_1m_candles() -> None:
    b = CandleBuilder(timeframes=[1, 15])
    got: list[Candle] = []
    b.add_listener(got.append)
    prices = [100, 110, 90, 105, 95, 120, 80, 100, 101, 102, 103, 104, 99, 98, 97, 96]
    for i, p in enumerate(prices):
        b.on_tick(tick(T0 + timedelta(minutes=i, seconds=30), p))
    ones = [c for c in got if c.timeframe_minutes == 1]
    (fifteen,) = [c for c in got if c.timeframe_minutes == 15]
    assert fifteen.high == max(c.high for c in ones) == 120
    assert fifteen.low == min(c.low for c in ones) == 80


def test_late_tick_is_dropped_not_rewriting_history() -> None:
    b = CandleBuilder(timeframes=[1])
    b.on_tick(tick(at(9, 15, 10), 100))
    (closed,) = b.on_tick(tick(at(9, 16, 5), 101))
    assert b.on_tick(tick(at(9, 15, 50), 500)) == []  # late tick for closed bucket
    assert b.dropped_late == 1
    assert closed.high == 100
    cur = b.current(K, 1)
    assert cur is not None and cur.high == 101 and not cur.complete


def test_ticks_outside_session_ignored() -> None:
    b = CandleBuilder()
    assert b.on_tick(tick(at(9, 14, 59), 100)) == []
    assert b.on_tick(tick(at(15, 30), 100)) == []
    assert b.ignored_outside_session == 2
    assert b.current(K, 1) is None


def test_flush_closes_candles_without_next_tick() -> None:
    b = CandleBuilder(timeframes=[1, 5])
    b.on_tick(tick(at(9, 15, 30), 100))
    assert b.flush(at(9, 15, 59)) == []
    closed = b.flush(at(9, 16))
    assert [c.timeframe_minutes for c in closed] == [1]
    closed = b.flush(at(9, 20))
    assert [c.timeframe_minutes for c in closed] == [5]
    assert b.flush(at(10, 0)) == []


def test_instruments_are_independent() -> None:
    b = CandleBuilder(timeframes=[1])
    b.on_tick(tick(at(9, 15, 1), 100, key="A"))
    b.on_tick(tick(at(9, 15, 2), 200, key="B"))
    out = b.on_tick(tick(at(9, 16, 1), 101, key="A"))
    assert [c.instrument_key for c in out] == ["A"]
    assert b.current("B", 1) is not None


def test_new_day_resets_volume_baseline() -> None:
    b = CandleBuilder(timeframes=[1])
    b.on_tick(tick(at(15, 29, 0), 100, vtt=900_000))
    next_day = datetime(2026, 11, 4, 9, 15, 5, tzinfo=IST)
    (prev,) = b.on_tick(tick(next_day, 100, vtt=1_000))
    b.on_tick(tick(next_day + timedelta(seconds=10), 100, vtt=1_500))
    (c,) = b.flush(next_day + timedelta(minutes=1))
    assert prev.start.date().day == 3
    assert c.volume == 500


def test_invalid_timeframes() -> None:
    with pytest.raises(ValueError):
        CandleBuilder(timeframes=[0])

# --------------------------------------------------------------------------- #
# Flush vs exchange time (local clock ahead of the exchange)
# --------------------------------------------------------------------------- #


def test_tick_after_flush_never_republishes_the_bucket() -> None:
    """A bucket closed by the timer is published once: a later tick that still
    belongs to it (exchange time behind the local clock) is dropped as late,
    not turned into a second candle for the same minute."""
    b = CandleBuilder(timeframes=[1])
    got: list[Candle] = []
    b.add_listener(got.append)
    b.on_tick(tick(at(9, 15, 50), 100))
    (closed,) = b.flush(at(9, 16, 0))
    assert b.on_tick(tick(at(9, 15, 55), 500)) == []
    assert b.flush(at(9, 16, 1)) == []
    assert b.current(K, 1) is None and b.dropped_late == 1
    assert [c.start for c in got] == [at(9, 15)] and closed.high == 100


def test_local_clock_seven_seconds_ahead_gives_one_candle_per_bucket() -> None:
    """The 2026-11-06 PAPER session: the PC clock ran ~7 s ahead and the 1 s
    flush timer produced ~8 'complete' candles per minute. Now each bucket is
    published exactly once, for every timeframe."""
    b = CandleBuilder(timeframes=[1, 3, 5], flush_grace_seconds=2.0)
    got: list[Candle] = []
    b.add_listener(got.append)
    skew = timedelta(seconds=7)
    exch = at(9, 15, 0)
    px = 100.0
    while exch < at(9, 30):
        b.on_tick(tick(exch, px))
        b.flush(exch + skew)  # the 1 s timer runs on the (fast) local clock
        exch += timedelta(seconds=1)
        px += 0.5
    for tf, expected in ((1, 15), (3, 5), (5, 3)):  # 09:15-09:29, all closed by the last flush
        starts = [c.start for c in got if c.timeframe_minutes == tf]
        assert len(starts) == len(set(starts)) == expected, (tf, starts)
    assert b.dropped_late > 0


def test_flush_grace_delays_the_timer_close() -> None:
    b = CandleBuilder(timeframes=[1], flush_grace_seconds=2.0)
    b.on_tick(tick(at(9, 15, 30), 100))
    assert b.flush(at(9, 16, 1)) == []
    b.on_tick(tick(at(9, 15, 59), 104))  # last tick of the bucket arrives within the grace
    (c,) = b.flush(at(9, 16, 2))
    assert c.close == 104 and c.tick_count == 2


def test_negative_flush_grace_rejected() -> None:
    with pytest.raises(ValueError):
        CandleBuilder(flush_grace_seconds=-1)
