"""Daily candles on the Upstox REST adapter (read-only, rate-limited, mocked transport)."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from broker.errors import RateLimitError, TokenScopeError
from broker.upstox_rest import DAILY_MAX_YEARS_PER_REQUEST, MINUTES_PER_DAY, is_market_data_path
from core.clock import IST
from core.enums import TradingMode, UpstoxTokenType
from tests.fakes import FakeWallClock
from tests.unit.test_upstox_rest import Harness, err, ok

VIX = "NSE_INDEX|India VIX"
ROWS = [["2026-11-13T00:00:00+05:30", 12.1, 12.9, 11.8, 12.4, 0, 0],
        ["2026-11-12T00:00:00+05:30", 11.9, 12.5, 11.7, 12.1, 0, 0]]


async def test_daily_candles_use_the_v3_days_path_with_to_date_first() -> None:
    h = Harness(lambda r: ok({"candles": ROWS}), token_type=UpstoxTokenType.ANALYTICS,
                now=FakeWallClock(datetime(2026, 11, 14, 10, 0, tzinfo=IST)))
    candles = await h.client.get_daily_candles(VIX, date(2026, 11, 1), date(2026, 11, 13))
    assert h.requests[-1].url.path == "/v3/historical-candle/NSE_INDEX|India VIX/days/1/2026-11-13/2026-11-01"
    assert is_market_data_path(h.requests[-1].url.path)
    assert [c.start.date() for c in candles] == [date(2026, 11, 12), date(2026, 11, 13)]  # oldest first
    assert all(c.timeframe_minutes == MINUTES_PER_DAY and c.complete for c in candles)
    assert candles[1].close == 12.4


async def test_daily_candles_go_through_the_central_rate_limiter() -> None:
    h = Harness(lambda r: ok({"candles": []}), token_type=UpstoxTokenType.ANALYTICS)
    for _ in range(3):
        await h.client.get_daily_candles(VIX, date(2025, 1, 1), date(2025, 12, 31))
    assert h.limiter.usage()["standard_api"]["granted"] == 3


async def test_todays_daily_candle_is_incomplete() -> None:
    h = Harness(lambda r: ok({"candles": ROWS[:1]}), now=FakeWallClock(datetime(2026, 11, 13, 14, 0, tzinfo=IST)))
    (candle,) = await h.client.get_daily_candles(VIX, date(2026, 11, 13), date(2026, 11, 13))
    assert not candle.complete


async def test_daily_candles_refuse_bad_ranges_before_sending() -> None:
    h = Harness(lambda r: ok({"candles": []}))
    with pytest.raises(ValueError, match="after"):
        await h.client.get_daily_candles(VIX, date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(ValueError, match="years per request"):
        await h.client.get_daily_candles(VIX, date(2000, 1, 1), date(2000 + DAILY_MAX_YEARS_PER_REQUEST, 1, 1))
    with pytest.raises(ValueError, match="years per request"):  # 29 Feb start
        await h.client.get_daily_candles(VIX, date(2004, 2, 29), date(2014, 2, 28))
    assert h.requests == []
    # one day short of the limit is fine
    await h.client.get_daily_candles(VIX, date(2000, 1, 1), date(2009, 12, 31))
    assert len(h.requests) == 1


async def test_daily_candles_retry_429_then_give_up() -> None:
    h = Harness(lambda r: err(429, "UDAPI10005"), max_retries=1)
    with pytest.raises(RateLimitError):
        await h.client.get_daily_candles(VIX, date(2026, 1, 1), date(2026, 1, 31))
    assert h.limiter.usage()["standard_api"]["total_429"] == 2


async def test_daily_candles_are_read_only_and_never_route_orders() -> None:
    h = Harness(lambda r: ok({"candles": []}), mode=TradingMode.PAPER, token_type=UpstoxTokenType.ANALYTICS)
    await h.client.get_daily_candles(VIX, date(2026, 1, 1), date(2026, 1, 31))
    assert {r.method for r in h.requests} == {"GET"}
    # the analytics-token scope guard still refuses account paths on the same client
    with pytest.raises(TokenScopeError):
        await h.client.get_funds()
    assert len(h.requests) == 1


def test_empty_response_is_an_empty_list() -> None:
    import asyncio

    h = Harness(lambda r: httpx.Response(200, json={"status": "success", "data": {}}))
    assert asyncio.run(h.client.get_daily_candles(VIX, date(2026, 1, 1), date(2026, 1, 2))) == []
