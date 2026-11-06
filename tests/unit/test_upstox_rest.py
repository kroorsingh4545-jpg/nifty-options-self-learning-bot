"""Upstox REST adapter against a mocked transport (no real network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

import httpx
import pytest

from broker.errors import (
    BrokerAPIError,
    BrokerTimeoutError,
    LiveTradingDisabledError,
    MaintenanceWindowError,
    RateLimitError,
    TokenExpiredError,
)
from broker.upstox_rate_limiter import ApiCategory, UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient, next_token_expiry
from config.loader import load_config
from config.settings import BrokerSection, CircuitBreakerSection
from core.clock import IST
from core.enums import TradingMode, UpstoxTokenType
from core.masking import SecretMasker
from tests.fakes import FakeClock, FakeWallClock, chain_row, option_contract

TOKEN = "test-access-token-0123456789abcdefXYZ"
EFFECTIVE = load_config(use_default_env_file=False).risk.rate_limits.effective
Handler = Callable[[httpx.Request], httpx.Response]


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def err(status: int, code: str, message: str = "error", headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"status": "error", "errors": [{"error_code": code, "message": message, "property_path": None,
                                            "invalid_value": None}]}
    return httpx.Response(status, json=body, headers=headers)


class Harness:
    def __init__(self, handler: Handler, *, mode: TradingMode = TradingMode.PAPER, token: str | None = TOKEN,
                 now: FakeWallClock | None = None, token_type: UpstoxTokenType = UpstoxTokenType.DAILY,
                 **settings: Any) -> None:
        self.requests: list[httpx.Request] = []
        self.clock = FakeClock()
        self.auth_failures: list[str] = []

        def record(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return handler(req)

        self.limiter = UpstoxRateLimiter(EFFECTIVE, CircuitBreakerSection(), clock=self.clock, queue_timeout=1000)
        self.http = httpx.AsyncClient(base_url="https://api.upstox.com", transport=httpx.MockTransport(record))
        self.masker = SecretMasker()
        self.masker.register(TOKEN)
        self.client = UpstoxRestClient(
            token_provider=lambda: token,
            limiter=self.limiter,
            mode=mode,
            token_type=token_type,
            settings=BrokerSection(**settings),
            http=self.http,
            clock=self.clock,
            masker=self.masker,
            now_fn=now or FakeWallClock(),
            on_auth_failure=self.auth_failures.append,
        )


# --------------------------------------------------------------------------- #
# Requests & parsing
# --------------------------------------------------------------------------- #


async def test_auth_header_and_endpoint() -> None:
    h = Harness(lambda r: ok([option_contract(24500, "CE"), option_contract(24500, "PE")]))
    contracts = await h.client.get_option_contracts("NSE_INDEX|Nifty 50", date(2026, 11, 10))
    req = h.requests[0]
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.url.path == "/v2/option/contract"
    assert req.url.params["instrument_key"] == "NSE_INDEX|Nifty 50"
    assert req.url.params["expiry_date"] == "2026-11-10"
    assert contracts[0].lot_size == 75
    assert contracts[0].tick_size == pytest.approx(0.05)  # paise -> rupees
    assert contracts[0].expiry == date(2026, 11, 10)
    assert contracts[1].option_type and contracts[1].option_type.value == "PE"


async def test_every_request_goes_through_rate_limiter() -> None:
    h = Harness(lambda r: ok([]))
    for _ in range(45):
        await h.client.get_positions()
    usage = h.limiter.usage()["standard_api"]
    assert usage["granted"] == 45
    assert h.clock.t > 1000  # the 41st request had to wait for the next second


async def test_option_chain_and_ltp_and_candles() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/option/chain":
            return ok([chain_row(24500, 24510.0)])
        if req.url.path == "/v3/market-quote/ltp":
            keys = req.url.params["instrument_key"].split(",")
            return ok({f"NSE_FO:SYM{i}": {"last_price": 100.0 + i, "instrument_token": k} for i, k in enumerate(keys)})
        if req.url.path.startswith("/v3/historical-candle/intraday/"):
            return ok({"candles": [["2026-11-03T09:16:00+05:30", 101, 103, 100, 102, 5000, 0],
                                   ["2026-11-03T09:15:00+05:30", 100, 102, 99, 101, 7000, 0]]})
        return httpx.Response(404)

    h = Harness(handler, now=FakeWallClock(datetime(2026, 11, 3, 9, 16, 30, tzinfo=IST)))
    raw = await h.client.get_option_chain("NSE_INDEX|Nifty 50", date(2026, 11, 10))
    assert raw["rows"][0]["strike_price"] == 24500

    keys = [f"NSE_FO|{i}" for i in range(650)]
    ltp = await h.client.get_ltp(keys)
    assert len(ltp) == 650
    ltp_calls = [r for r in h.requests if r.url.path == "/v3/market-quote/ltp"]
    assert len(ltp_calls) == 2  # chunked at 500

    candles = await h.client.get_intraday_candles("NSE_INDEX|Nifty 50", 1)
    assert [c.start.minute for c in candles] == [15, 16]  # oldest first
    assert candles[0].complete and not candles[1].complete  # 09:16 candle still forming
    assert "NSE_INDEX%7CNifty%2050" in str(h.requests[-1].url)


async def test_positions_and_funds_parsing() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/portfolio/short-term-positions":
            return ok([{"instrument_token": "NSE_FO|1", "trading_symbol": "NIFTY 24500 CE", "product": "I",
                        "quantity": 75, "average_price": 120.5, "last_price": 130.0, "realised": 0,
                        "unrealised": 712.5, "day_buy_quantity": 75, "day_sell_quantity": 0}])
        return ok({"equity": {"used_margin": 9037.5, "available_margin": 190962.5, "payin_amount": 0,
                              "notional_cash": 0, "span_margin": 0, "adhoc_margin": 0, "exposure_margin": 0}})

    h = Harness(handler)
    (pos,) = await h.client.get_positions()
    assert pos.instrument_key == "NSE_FO|1" and pos.quantity == 75 and pos.buy_quantity == 75
    funds = await h.client.get_funds()
    assert funds.available_margin == pytest.approx(190962.5)
    assert h.requests[-1].url.params["segment"] == "SEC"


async def test_feed_authorize_endpoints() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return ok({"authorized_redirect_uri": f"wss://x.upstox.com{req.url.path}?code=abc"})

    h = Harness(handler)
    assert (await h.client.authorize_market_feed()).startswith("wss://")
    assert h.requests[-1].url.path == "/v3/feed/market-data-feed/authorize"
    await h.client.authorize_portfolio_feed(["order", "position"])
    assert h.requests[-1].url.path == "/v2/feed/portfolio-stream-feed/authorize"
    assert h.requests[-1].url.params["update_types"] == "order,position"


# --------------------------------------------------------------------------- #
# Retries, timeouts, 429
# --------------------------------------------------------------------------- #


async def test_get_retries_on_5xx_with_backoff() -> None:
    responses = [httpx.Response(503), httpx.Response(502), ok([])]
    h = Harness(lambda r: responses.pop(0))
    assert await h.client.get_positions() == []
    assert len(h.requests) == 3
    assert len(h.clock.sleeps) >= 2


async def test_get_gives_up_after_max_retries() -> None:
    h = Harness(lambda r: httpx.Response(500, json={"status": "error", "errors": []}), max_retries=2)
    with pytest.raises(BrokerAPIError):
        await h.client.get_positions()
    assert len(h.requests) == 3


async def test_timeout_retried_then_raises_timeout_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    h = Harness(handler, max_retries=2)
    with pytest.raises(BrokerTimeoutError):
        await h.client.get_positions()
    assert len(h.requests) == 3


async def test_network_error_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=req)
        return ok([])

    h = Harness(handler)
    assert await h.client.get_positions() == []


async def test_429_records_circuit_and_retries_after_cooldown() -> None:
    responses = [err(429, "UDAPI10005", headers={"Retry-After": "3"}), ok([])]
    h = Harness(lambda r: responses.pop(0))
    start = h.clock.t
    assert await h.client.get_positions() == []
    assert h.clock.t - start >= 3  # waited Retry-After before resending
    assert h.limiter.usage()["standard_api"]["total_429"] == 1


async def test_persistent_429_raises_rate_limit_error() -> None:
    h = Harness(lambda r: err(429, "UDAPI10005"), max_retries=2)
    with pytest.raises(RateLimitError):
        await h.client.get_positions()
    assert len(h.requests) == 3


# --------------------------------------------------------------------------- #
# Auth / token expiry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("response", [httpx.Response(401), err(401, "UDAPI100050", "Invalid token"),
                                      err(400, "UDAPI100050", "Invalid token")])
async def test_invalid_token_fails_fast_without_looping(response: httpx.Response) -> None:
    h = Harness(lambda r: response)
    with pytest.raises(TokenExpiredError):
        await h.client.get_positions()
    with pytest.raises(TokenExpiredError):
        await h.client.get_funds()
    assert len(h.requests) == 1  # second call never hit the network
    assert len(h.auth_failures) == 1


async def test_missing_token_never_calls_network() -> None:
    h = Harness(lambda r: ok([]), token=None)
    with pytest.raises(TokenExpiredError, match="not configured"):
        await h.client.get_positions()
    assert h.requests == []


async def test_daily_token_expiry_is_enforced_and_reset() -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 9, 0, tzinfo=IST))
    h = Harness(lambda r: ok([]), now=wall)
    assert h.client.token_expires_at == datetime(2026, 11, 4, 3, 30, tzinfo=IST)
    await h.client.get_positions()
    wall.now = datetime(2026, 11, 4, 3, 31, tzinfo=IST)
    with pytest.raises(TokenExpiredError, match="expiry"):
        await h.client.get_positions()
    assert len(h.requests) == 1
    h.client.reset_auth()
    await h.client.get_positions()
    assert len(h.requests) == 2


def test_next_token_expiry() -> None:
    t = time(3, 30)
    assert next_token_expiry(datetime(2026, 11, 3, 2, 0, tzinfo=IST), t) == datetime(2026, 11, 3, 3, 30, tzinfo=IST)
    assert next_token_expiry(datetime(2026, 11, 3, 3, 30, tzinfo=IST), t) == datetime(2026, 11, 4, 3, 30, tzinfo=IST)


async def test_maintenance_window_error() -> None:
    h = Harness(lambda r: err(423, "UDAPI100072", "Service unavailable 12AM-5:30AM"))
    with pytest.raises(MaintenanceWindowError):
        await h.client.get_funds()


async def test_api_error_message_is_masked() -> None:
    h = Harness(lambda r: err(400, "UDAPI100011", f"bad request for token {TOKEN}"))
    with pytest.raises(BrokerAPIError) as info:
        await h.client.get_option_chain("BAD", date(2026, 11, 10))
    assert TOKEN not in str(info.value)
    assert info.value.error_code == "UDAPI100011"


async def test_token_never_logged(capsys: pytest.CaptureFixture[str]) -> None:
    import io

    from monitoring.json_logger import configure_logging

    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf)
    try:
        responses = [httpx.Response(503), err(429, "UDAPI10005"), httpx.Response(401)]
        h = Harness(lambda r: responses.pop(0))
        with pytest.raises(TokenExpiredError):
            await h.client.get_positions()
    finally:
        configure_logging(console=False)
    assert TOKEN not in buf.getvalue()
    assert "upstox authentication failed" in buf.getvalue()


# --------------------------------------------------------------------------- #
# Orders are refused
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", list(TradingMode))
@pytest.mark.parametrize("method", ["place_order", "modify_order", "cancel_order"])
async def test_order_methods_refuse_and_never_touch_network(mode: TradingMode, method: str) -> None:
    h = Harness(lambda r: ok({"order_id": "1"}), mode=mode)
    with pytest.raises(LiveTradingDisabledError):
        await getattr(h.client, method)({"quantity": 75})
    assert h.requests == []
    assert h.limiter.usage()[ApiCategory.ORDER.value]["granted"] == 0


def test_error_body_shape_handling() -> None:
    from broker.upstox_rest import _first_error

    assert _first_error({"status": "error", "errors": [{"error_code": "X", "message": "m"}]}) == ("X", "m")
    assert _first_error({"status": "error", "errors": [{"errorCode": "Y", "message": "n"}]}) == ("Y", "n")
    assert _first_error(json.loads("[]"))[0] is None
