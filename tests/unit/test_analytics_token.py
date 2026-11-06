"""Upstox Analytics Token support (UPSTOX_TOKEN_TYPE=analytics).

An analytics token is read-only and, without a static IP, only reaches the
market-data APIs. PAPER must work with it while calling market-data endpoints
only; LIVE must always refuse it. No test connects to Upstox.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from broker.errors import LiveTradingDisabledError, TokenExpiredError, TokenScopeError
from broker.factory import create_broker
from broker.upstox_rest import MARKET_DATA_PATH_PREFIXES, is_market_data_path
from config import hard_limits
from config.loader import ConfigError, LiveModeNotPermittedError, load_config
from core.clock import IST
from core.enums import TradingMode, UpstoxTokenType
from core.mode_controller import LIVE_CONFIRMATION_PHRASE, LivePreconditions, ModeController
from runtime import __main__ as cli
from runtime.paper import RiskHooks, preflight, upstox_connections
from tests.fakes import FakeWallClock
from tests.paper_support import paper_cfg
from tests.unit.test_upstox_rest import TOKEN, Harness, ok

ANALYTICS = UpstoxTokenType.ANALYTICS
FAKE_TOKEN = "fake-analytics-token-ZYXWVUTSRQPONMLKJIHG"


def _env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "test.env"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Setting
# --------------------------------------------------------------------------- #


def test_token_type_defaults_to_analytics_and_is_normalised(config_dir: Path, tmp_path: Path) -> None:
    cfg = load_config(config_dir, env_file=_env(tmp_path, f"UPSTOX_ACCESS_TOKEN={FAKE_TOKEN}\n"))
    assert cfg.env.upstox_token_type is ANALYTICS
    cfg = load_config(config_dir, env_file=_env(tmp_path, "UPSTOX_TOKEN_TYPE= Daily \n"))
    assert cfg.env.upstox_token_type is UpstoxTokenType.DAILY


def test_unknown_token_type_is_refused(config_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="upstox_token_type"):
        load_config(config_dir, env_file=_env(tmp_path, "UPSTOX_TOKEN_TYPE=extended\n"))


def test_analytics_token_needs_no_api_key_but_a_daily_token_does(config_dir: Path, tmp_path: Path) -> None:
    only_token = f"UPSTOX_ACCESS_TOKEN={FAKE_TOKEN}\n"
    assert load_config(config_dir, env_file=_env(tmp_path, only_token)).env.upstox_credentials_present
    daily = load_config(config_dir, env_file=_env(tmp_path, only_token + "UPSTOX_TOKEN_TYPE=daily\n"))
    assert not daily.env.upstox_credentials_present
    assert not load_config(config_dir, env_file=_env(tmp_path, "")).env.upstox_credentials_present


def test_preflight_describes_the_analytics_token(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path, upstox_api_key=None)
    (cred,) = [c for c in preflight(cfg) if c.name == "Upstox credentials"]
    assert cred.ok and "analytics" in cred.detail and "market-data" in cred.detail


# --------------------------------------------------------------------------- #
# Market-data-only REST client
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [
    "/v2/market-quote/quotes", "/v3/market-quote/ltp", "/v3/market-quote/option-greek",
    "/v2/historical-candle/NSE_INDEX%7CNifty%2050/day/2026-11-05",
    "/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/1",
    "/v2/option/contract", "/v2/option/chain", "/v2/market/holidays", "/v2/market/timings/2026-11-06",
    "/v3/feed/market-data-feed/authorize",
])
def test_market_data_paths_are_allowed(path: str) -> None:
    assert is_market_data_path(path)


@pytest.mark.parametrize("path", [
    "/v2/user/profile", "/v2/user/get-funds-and-margin", "/v2/portfolio/short-term-positions",
    "/v2/portfolio/long-term-holdings", "/v2/order/retrieve-all", "/v2/order/place", "/v3/order/place",
    "/v2/feed/portfolio-stream-feed/authorize", "/v2/trade/profit-loss/data", "/v2/charges/brokerage",
    "/v2/market-quote", "/v2/options/chain",
])
def test_account_and_order_paths_are_not_market_data(path: str) -> None:
    assert not is_market_data_path(path)


def test_allowlist_is_only_market_data() -> None:
    for prefix in MARKET_DATA_PATH_PREFIXES:
        assert not any(word in prefix for word in ("user", "portfolio", "order", "trade", "charges"))


async def test_analytics_client_refuses_account_calls_without_touching_the_network() -> None:
    h = Harness(lambda r: ok([]), token_type=ANALYTICS)
    calls = (h.client.get_funds, h.client.get_positions, h.client.get_order_book,
             lambda: h.client.authorize_portfolio_feed(["order"]))
    for call in calls:
        with pytest.raises(TokenScopeError, match="market-data"):
            await call()
    assert h.requests == []  # nothing was sent
    assert h.limiter.usage()["standard_api"]["granted"] == 0  # no rate-limit slot used
    assert h.client.auth_failed is None and h.auth_failures == []  # a scope refusal is not an auth failure


async def test_analytics_client_calls_market_data_endpoints() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v3/market-quote/ltp":
            return ok({"NSE_INDEX:Nifty 50": {"last_price": 24500.5, "instrument_token": "NSE_INDEX|Nifty 50"}})
        if req.url.path == "/v3/feed/market-data-feed/authorize":
            return ok({"authorized_redirect_uri": "wss://x.upstox.com/feed?code=abc"})
        if req.url.path == "/v2/option/chain":
            return ok([])
        return httpx.Response(404)

    h = Harness(handler, token_type=ANALYTICS)
    assert await h.client.get_ltp(["NSE_INDEX|Nifty 50"]) == {"NSE_INDEX|Nifty 50": 24500.5}
    assert (await h.client.authorize_market_feed()).startswith("wss://")

    assert (await h.client.get_option_chain("NSE_INDEX|Nifty 50", date(2026, 11, 10)))["rows"] == []
    assert all(is_market_data_path(r.url.path) for r in h.requests)
    assert h.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_analytics_token_has_no_daily_expiry_but_401_still_fails_auth() -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 9, 0, tzinfo=IST))
    h = Harness(lambda r: ok({}), token_type=ANALYTICS, now=wall)
    assert h.client.token_expires_at is None
    wall.advance(days=30)  # well past any 03:30 daily expiry
    await h.client.get_ltp(["NSE_INDEX|Nifty 50"])
    assert h.client.auth_failed is None

    h401 = Harness(lambda r: httpx.Response(401, json={"status": "error", "errors": [
        {"error_code": "UDAPI100050", "message": "Invalid token used to access API"}]}), token_type=ANALYTICS)

    with pytest.raises(TokenExpiredError):
        await h401.client.get_ltp(["NSE_INDEX|Nifty 50"])
    assert h401.auth_failures


async def test_paper_connections_use_the_configured_token_type(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    conn = upstox_connections(cfg, RiskHooks(lambda _: None, lambda _: None))
    try:
        assert conn.source.token_type is ANALYTICS  # type: ignore[attr-defined]
        assert getattr(conn.feed, "portfolio", "missing") is None
        with pytest.raises(TokenScopeError):
            await conn.source.get_funds()  # type: ignore[attr-defined]
    finally:
        assert conn.aclose is not None
        await conn.aclose()


# --------------------------------------------------------------------------- #
# check --connect uses a market-data call
# --------------------------------------------------------------------------- #


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: object, **env: object) -> None:
    cfg = paper_cfg(tmp_path, **env)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "configure_from_config", lambda _cfg: None)
    real = cli.UpstoxRestClient.from_config

    def fake_from_config(c: object, limiter: object, **kw: object) -> object:
        return real(c, limiter, http=httpx.AsyncClient(base_url="https://api.upstox.com",  # type: ignore[arg-type]
                                                       transport=httpx.MockTransport(handler)), **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(cli.UpstoxRestClient, "from_config", staticmethod(fake_from_config))


def test_check_connect_makes_one_market_data_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return ok({"NSE_INDEX:Nifty 50": {"last_price": 24512.35, "instrument_token": "NSE_INDEX|Nifty 50"}})

    _cli(tmp_path, monkeypatch, handler, upstox_api_key=None, upstox_access_token=SecretStr("super-secret-xyz"))
    cli.main(["check", "--connect"])
    out = capsys.readouterr().out
    assert seen == ["/v3/market-quote/ltp"]
    assert "[PASS] Upstox token (analytics)" in out and "24,512.35" in out
    assert "super-secret-xyz" not in out


@pytest.mark.parametrize("token_type", [UpstoxTokenType.ANALYTICS, UpstoxTokenType.DAILY])
def test_check_connect_uses_market_data_for_both_token_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str],
                                                             token_type: UpstoxTokenType) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(401, json={"status": "error", "errors": [{"error_code": "UDAPI100050",
                                                                         "message": "Invalid token"}]})

    _cli(tmp_path, monkeypatch, handler, upstox_token_type=token_type)
    assert cli.main(["check", "--connect"]) == 1
    out = capsys.readouterr().out
    assert f"[FAIL] Upstox token ({token_type.value}): TokenExpiredError" in out
    assert seen == ["/v3/market-quote/ltp"]


def test_check_connect_without_credentials_reports_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    _cli(tmp_path, monkeypatch, lambda r: ok({}), upstox_access_token=None)
    assert cli.main(["check", "--connect"]) == 1
    assert "[FAIL] Upstox token: not checked" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# LIVE always refuses an analytics token
# --------------------------------------------------------------------------- #


def test_live_with_analytics_token_is_refused_even_if_live_were_available(
        config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hard_limits, "LIVE_TRADING_AVAILABLE", True)
    with pytest.raises(LiveModeNotPermittedError, match="analytics"):
        load_config(config_dir, env_file=_env(tmp_path, "TRADING_MODE=LIVE\nUPSTOX_TOKEN_TYPE=analytics\n"))
    with pytest.raises(LiveModeNotPermittedError, match="analytics"):  # the default token type
        load_config(config_dir, env_file=_env(tmp_path, "TRADING_MODE=LIVE\n"))


def test_live_broker_factory_refuses_an_analytics_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = paper_cfg(tmp_path)
    live = cfg.model_copy(update={"env": cfg.env.model_copy(update={"trading_mode": TradingMode.LIVE})})
    monkeypatch.setattr(hard_limits, "LIVE_TRADING_AVAILABLE", True)
    with pytest.raises(LiveTradingDisabledError, match="Analytics Token"):
        create_broker(live, quotes=lambda _k: None)  # type: ignore[arg-type, return-value]


async def test_rest_order_methods_name_the_analytics_token_in_live() -> None:
    h = Harness(lambda r: ok({}), mode=TradingMode.LIVE, token_type=ANALYTICS)
    for call in (h.client.place_order, h.client.modify_order, h.client.cancel_order):
        with pytest.raises(LiveTradingDisabledError, match="Analytics Token"):
            await call()
    assert h.requests == []


def _all_met() -> LivePreconditions:
    return LivePreconditions(reconciliation_ok=True, kill_switch_armed=True, kill_switch_active=False,
                             loss_limits_configured=True, paper_validation_passed=True, approved_by="operator",
                             pending_orders=0, broker_connected=True)


def test_mode_controller_refuses_live_with_an_analytics_token() -> None:
    mc = ModeController(live_available=True, token_type=ANALYTICS)
    res = mc.request_live(LIVE_CONFIRMATION_PHRASE, _all_met())
    assert not res.approved and mc.mode is TradingMode.PAPER
    assert any("Analytics Token" in r for r in res.reasons)
    assert ModeController(live_available=True, token_type=UpstoxTokenType.DAILY).evaluate_live(
        LIVE_CONFIRMATION_PHRASE, _all_met()) == []
