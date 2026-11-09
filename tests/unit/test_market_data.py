"""MarketDataService: caching, staleness, reconnect invalidation, REST recovery."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from broker.broker_interface import MarketDataSource
from core.models import Candle, Instrument, Tick
from data.market_data import MarketDataService
from tests.fakes import T0, FakeWallClock


def tick(key: str, ltp: float, wall: FakeWallClock, offset_s: float = 0) -> Tick:
    t = wall.now + timedelta(seconds=offset_s)
    return Tick(instrument_key=key, ltp=ltp, exchange_time=t, received_at=wall.now)


class FakeSource(MarketDataSource):
    def __init__(self) -> None:
        self.ltp_calls = 0

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        return []

    async def get_instruments(self, underlying_symbols: Sequence[str] = ("NIFTY",)) -> list[Instrument]:
        return []

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        return {}

    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        self.ltp_calls += 1
        return {k: 50.0 for k in instrument_keys}

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        return []

    async def get_historical_candles(self, instrument_key: str, interval_minutes: int, from_date: date,
                                     to_date: date) -> list[Candle]:
        return []


def test_latest_tick_and_listeners() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    seen: list[str] = []
    md.add_listener(lambda t: seen.append(t.instrument_key))
    md.on_ticks([tick("A", 1, wall), tick("B", 2, wall)])
    assert md.ltp("A") == 1 and md.get("B") is not None
    assert seen == ["A", "B"]


def test_out_of_order_tick_ignored() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    md.on_tick(tick("A", 10, wall, offset_s=5))
    md.on_tick(tick("A", 99, wall, offset_s=1))
    assert md.ltp("A") == 10 and md.out_of_order == 1


def test_freshness_by_age() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(stale_after_seconds=5, now_fn=wall)
    md.on_tick(tick("A", 1, wall))
    assert md.is_fresh("A")
    wall.advance(seconds=6)
    assert not md.is_fresh("A")
    assert md.stale_instruments() == ["A"]
    assert not md.is_fresh("missing")


def test_reconnect_marks_stale_until_fresh_tick() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    md.on_ticks([tick("A", 1, wall), tick("B", 1, wall)])
    md.mark_all_stale()
    assert not md.is_fresh("A") and not md.is_fresh("B")
    md.on_tick(tick("A", 2, wall, offset_s=1))
    assert md.is_fresh("A") and not md.is_fresh("B")


def test_listener_error_does_not_break_fanout() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    got: list[float] = []

    def bad(_: Tick) -> None:
        raise RuntimeError("boom")

    md.add_listener(bad)
    md.add_listener(lambda t: got.append(t.ltp))
    md.on_tick(tick("A", 7, wall))
    assert got == [7]


async def test_rest_snapshot_is_debounced_and_never_fresh() -> None:
    wall = FakeWallClock(T0)
    src = FakeSource()
    md = MarketDataService(rest=src, now_fn=wall, snapshot_debounce_seconds=60)
    await md.recover_snapshot(["A", "B"])
    await md.recover_snapshot(["B", "A"])
    assert src.ltp_calls == 1
    snap = md.get("A")
    assert snap is not None and snap.is_snapshot and snap.ltp == 50
    assert not md.is_fresh("A")  # snapshots never count as fresh for trading
    md.on_tick(tick("A", 51, wall))
    assert md.is_fresh("A")


async def test_snapshot_does_not_overwrite_live_tick() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(rest=FakeSource(), now_fn=wall)
    md.on_tick(tick("A", 42, wall))
    await md.recover_snapshot(["A"])
    assert md.ltp("A") == 42

def test_feed_lag_is_median_of_live_ticks_only() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    assert md.feed_lag("A") is None
    for lag in (7.0, 7.4, 8.5):  # the median ignores one slow message
        wall.now += timedelta(seconds=5)
        md.on_tick(Tick(instrument_key="A", ltp=1, exchange_time=wall.now - timedelta(seconds=lag),
                        received_at=wall.now))
    assert md.feed_lag("A") == 7.4
    md.on_tick(Tick(instrument_key="B", ltp=1, exchange_time=wall.now - timedelta(seconds=99),
                    received_at=wall.now, is_snapshot=True))
    assert md.feed_lag("B") is None


def test_quote_status_reports_price_freshness_and_age() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall, stale_after_seconds=10)
    assert md.quote_status("A") == {"ltp": None, "fresh": False, "age_seconds": None, "exchange_time": None,
                                    "snapshot": False}
    md.on_tick(tick("A", 24850.5, wall))
    q = md.quote_status("A")
    assert q["ltp"] == 24850.5 and q["fresh"] and q["age_seconds"] == 0 and not q["snapshot"]
    wall.now += timedelta(seconds=12)
    q = md.quote_status("A")
    assert not q["fresh"] and q["age_seconds"] == 12
