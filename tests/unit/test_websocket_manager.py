"""WebSocketManager wiring + broker factory guards."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from broker import upstox_websocket
from broker.errors import LiveTradingDisabledError
from broker.factory import create_broker
from broker.paper_broker import PaperBroker
from config.loader import load_config
from config.settings import WebSocketSection
from data.market_data import MarketDataService
from data.websocket_manager import WebSocketManager
from tests.fakes import T0, FakeAuthorizer, FakeConnector, FakeWallClock, feed_option

FAST = WebSocketSection(reconnect_base_seconds=0.001, reconnect_max_seconds=0.005, max_reconnect_attempts=3,
                        heartbeat_interval_seconds=0.05, heartbeat_timeout_seconds=0.05)


@pytest.fixture(autouse=True)
def _reset_active() -> Iterator[None]:
    upstox_websocket._ACTIVE_KINDS.clear()
    yield
    upstox_websocket._ACTIVE_KINDS.clear()


async def until(cond: object, timeout: float = 5.0) -> None:
    async def _p() -> None:
        while not cond():  # type: ignore[operator]
            await asyncio.sleep(0.002)

    await asyncio.wait_for(_p(), timeout)


async def test_paper_mode_opens_only_market_stream_and_routes_ticks() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    cf = FakeConnector()
    mgr = WebSocketManager(authorizer=FakeAuthorizer(), market_data=md, settings=FAST, connector=cf)
    assert mgr.portfolio is None
    await mgr.subscribe(["NSE_FO|1"])
    await mgr.start()
    conn = await cf.wait_for_connections(1)
    conn.push(feed_option("NSE_FO|1", 101.0, T0))
    await until(lambda: md.ltp("NSE_FO|1") == 101.0)
    h = mgr.health()
    assert h["market_data"]["connected"] and h["market_data"]["subscriptions"] == 1
    assert "portfolio" not in h
    await mgr.stop()
    assert cf.attempts == 1


async def test_disconnect_marks_data_stale_and_notifies() -> None:
    wall = FakeWallClock(T0)
    md = MarketDataService(now_fn=wall)
    cf = FakeConnector()
    events: list[tuple[str, str]] = []
    mgr = WebSocketManager(authorizer=FakeAuthorizer(), market_data=md, settings=FAST, connector=cf)
    mgr.add_status_listener(lambda k, o, n: events.append((k, n.value)))
    await mgr.subscribe(["NSE_FO|1"])
    await mgr.start()
    first = await cf.wait_for_connections(1)
    first.push(feed_option("NSE_FO|1", 101.0, T0))
    await until(lambda: md.is_fresh("NSE_FO|1"))
    first.drop()
    await until(lambda: ("market_data", "RECONNECTING") in events)
    assert not md.is_fresh("NSE_FO|1")  # stale until a new live tick
    second = await cf.wait_for_connections(2)
    second.push(feed_option("NSE_FO|1", 102.0, T0))
    await until(lambda: md.is_fresh("NSE_FO|1"))
    await mgr.stop()


async def test_live_enables_portfolio_stream() -> None:
    md = MarketDataService()
    cf = FakeConnector()
    mgr = WebSocketManager(authorizer=FakeAuthorizer(), market_data=md, settings=FAST, connector=cf,
                           enable_portfolio=True)
    await mgr.start()
    await cf.wait_for_connections(2)
    assert any("portfolio" in u for u in cf.urls) and any("market" in u for u in cf.urls)
    await mgr.stop()


async def test_health_reports_ticks_errors_and_last_message_age() -> None:
    cf = FakeConnector()
    ws = WebSocketManager(authorizer=FakeAuthorizer(), market_data=MarketDataService(), connector=cf)
    h = ws.health()["market_data"]
    assert h["ticks"] == 0 and h["message_errors"] == 0 and h["last_message_age_seconds"] is None
    assert h["connected"] is False


def test_factory_wires_paper_broker_in_paper_mode() -> None:
    cfg = load_config(use_default_env_file=False)
    broker = create_broker(cfg, quotes=lambda k: None)
    assert isinstance(broker, PaperBroker)


def test_factory_refuses_live() -> None:
    from core.enums import TradingMode

    cfg = load_config(use_default_env_file=False)
    # Bypass the loader's LIVE refusal to prove the factory refuses independently.
    live_cfg = cfg.model_copy(update={"env": cfg.env.model_copy(update={"trading_mode": TradingMode.LIVE})})
    assert live_cfg.mode is TradingMode.LIVE
    with pytest.raises(LiveTradingDisabledError):
        create_broker(live_cfg, quotes=lambda k: None)


def test_new_config_sections_load() -> None:
    cfg = load_config(use_default_env_file=False)
    s = cfg.settings
    assert s.broker.rest_base_url == "https://api.upstox.com"
    assert s.websocket.market_data_mode == "full"
    assert s.paper_broker.allow_short_options is False
    assert s.costs.stt_sell_pct == 0.15
