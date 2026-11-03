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
