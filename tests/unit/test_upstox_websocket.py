"""WebSocket adapters: connect, subscribe, reconnect + resubscribe, heartbeat,
auth failure, retry exhaustion, subscription limits, connection limit."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from broker import upstox_websocket
from broker.errors import ConnectionLimitError, TokenExpiredError
from broker.upstox_websocket import MarketDataStream, PortfolioStream, StreamState, SubscriptionLimitError
from config.settings import WebSocketSection
from core.enums import OrderStatus
from core.models import OrderUpdate, Tick
from tests.fakes import T0, FakeAuthorizer, FakeConnector, feed_option

FAST = WebSocketSection(
    reconnect_base_seconds=0.001,
    reconnect_max_seconds=0.005,
    max_reconnect_attempts=3,
    heartbeat_interval_seconds=0.02,
    heartbeat_timeout_seconds=0.02,
    stale_after_seconds=0.05,
    subscribe_chunk_size=2,
)


@pytest.fixture(autouse=True)
def _reset_active() -> Iterator[None]:
    upstox_websocket._ACTIVE_KINDS.clear()
    yield
    upstox_websocket._ACTIVE_KINDS.clear()


def sent_json(msgs: list[Any]) -> list[dict[str, Any]]:
    out = []
    for m in msgs:
        assert isinstance(m, bytes), "subscription requests must be binary frames"
        out.append(json.loads(m.decode()))
    return out


async def until(cond: Any, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not cond():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(_poll(), timeout)


def make_market(connector: FakeConnector, auth: FakeAuthorizer | None = None,
                settings: WebSocketSection = FAST) -> tuple[MarketDataStream, list[Tick], list[tuple[str, str]]]:
    ticks: list[Tick] = []
    states: list[tuple[str, str]] = []
    s = MarketDataStream(authorize=(auth or FakeAuthorizer()).authorize_market_feed, connector=connector,
                         settings=settings, on_ticks=ticks.extend)
    s.add_state_listener(lambda kind, old, new: states.append((old.value, new.value)))
    return s, ticks, states


# --------------------------------------------------------------------------- #


async def test_connect_subscribe_and_receive_ticks() -> None:
    conn_factory = FakeConnector()
    stream, ticks, _ = make_market(conn_factory)
    await stream.subscribe(["NSE_FO|1", "NSE_FO|2", "NSE_FO|3"], "full")  # before connect: just registered
    task = asyncio.create_task(stream.run())
    conn = await conn_factory.wait_for_connections(1)
    await until(lambda: stream.connected)
    msgs = sent_json(conn.sent)
    assert [m["method"] for m in msgs] == ["sub", "sub"]  # chunked by 2
    assert sorted(k for m in msgs for k in m["data"]["instrumentKeys"]) == ["NSE_FO|1", "NSE_FO|2", "NSE_FO|3"]
    assert all(m["data"]["mode"] == "full" and m["guid"] for m in msgs)

    conn.push(feed_option("NSE_FO|1", 120.0, T0))
    await until(lambda: len(ticks) == 1)
    assert ticks[0].ltp == 120.0 and ticks[0].bid == 119.5
    await stream.stop()
    await asyncio.wait_for(task, 1)
    assert stream.state is StreamState.STOPPED


async def test_live_subscribe_unsubscribe_and_change_mode() -> None:
    cf = FakeConnector()
    stream, _, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    conn = await cf.wait_for_connections(1)
    await until(lambda: stream.connected)
    await stream.subscribe(["A"], "ltpc")
    await stream.subscribe(["A"], "full")
    await stream.subscribe(["A"], "full")  # no-op
    await stream.unsubscribe(["A", "ZZ"])
    msgs = sent_json(conn.sent)
    assert [(m["method"], m["data"].get("mode")) for m in msgs] == [("sub", "ltpc"), ("change_mode", "full"),
                                                                     ("unsub", None)]
    assert stream.subscriptions == {}
    await stream.stop()
    await task


async def test_reconnect_reauthorizes_and_resubscribes_everything() -> None:
    cf = FakeConnector()
    auth = FakeAuthorizer()
    stream, ticks, states = make_market(cf, auth)
    reconnects: list[int] = []
    stream.add_reconnect_listener(lambda: reconnects.append(1))
    await stream.subscribe(["K1", "K2"], "full")
    await stream.subscribe(["IDX"], "ltpc")
    task = asyncio.create_task(stream.run())
    first = await cf.wait_for_connections(1)
    await until(lambda: stream.connected)
    first.drop()  # server drops the socket

    second = await cf.wait_for_connections(2)
    await until(lambda: stream.connected and stream.connect_count == 2)
    resent = sent_json(second.sent)
    resubscribed = {k: m["data"]["mode"] for m in resent for k in m["data"]["instrumentKeys"]}
    assert resubscribed == {"K1": "full", "K2": "full", "IDX": "ltpc"}
    assert auth.calls == 2 and cf.urls[0] != cf.urls[1]  # fresh single-use URL each time
    assert reconnects == [1]
    assert ("CONNECTED", "RECONNECTING") in states
    second.push(feed_option("K1", 99.0, T0))
    await until(lambda: len(ticks) == 1)
    await stream.stop()
    await task


async def test_connect_failures_back_off_then_succeed() -> None:
    cf = FakeConnector(fail_first=2)
    stream, _, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    await cf.wait_for_connections(1)
    await until(lambda: stream.connected)
    assert cf.attempts == 3
    assert stream.reconnect_attempts == 0  # reset after a successful connect
    await stream.stop()
    await task


async def test_gives_up_after_max_attempts_and_reports_failed() -> None:
    cf = FakeConnector(fail_first=100)
    stream, _, states = make_market(cf)
    await asyncio.wait_for(stream.run(), 2)
    assert stream.state is StreamState.FAILED
    assert cf.attempts == FAST.max_reconnect_attempts + 1
    assert states[-1] == ("RECONNECTING", "FAILED")


async def test_auth_failure_stops_without_retry_loop() -> None:
    cf = FakeConnector()
    auth = FakeAuthorizer(fail_with=TokenExpiredError("expired"))
    stream, _, _ = make_market(cf, auth)
    await asyncio.wait_for(stream.run(), 1)
    assert stream.state is StreamState.AUTH_FAILED
    assert auth.calls == 1
    assert cf.attempts == 0


async def test_heartbeat_timeout_triggers_reconnect() -> None:
    cf = FakeConnector(pong=False)  # server never answers pings
    stream, _, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    await cf.wait_for_connections(2, timeout=2)
    assert cf.connections[0].pings >= 1
    assert cf.connections[0].closed
    assert "HeartbeatTimeout" in (stream.last_error or "")
    await stream.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_heartbeat_pings_sent_while_healthy() -> None:
    cf = FakeConnector()
    stream, _, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    conn = await cf.wait_for_connections(1)
    await until(lambda: conn.pings >= 3)
    assert stream.connect_count == 1  # pongs arrive, no reconnect
    await stream.stop()
    await task


async def test_stale_detection_when_silent() -> None:
    cf = FakeConnector()
    stream, _, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    conn = await cf.wait_for_connections(1)
    await until(lambda: stream.connected)
    conn.push(feed_option("K", 1.0, T0))
    await until(lambda: stream.messages_received == 1)
    assert not stream.is_stale()
    await asyncio.sleep(0.08)
    assert stream.is_stale()
    await stream.stop()
    await task


async def test_bad_message_does_not_kill_connection() -> None:
    cf = FakeConnector()
    stream, ticks, _ = make_market(cf)
    task = asyncio.create_task(stream.run())
    conn = await cf.wait_for_connections(1)
    conn.push(b"\x00\xffnot-protobuf")
    conn.push(feed_option("K", 5.0, T0))
    await until(lambda: len(ticks) == 1)
    assert stream.connect_count == 1
    await stream.stop()
    await task


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


async def test_subscription_limits_enforced() -> None:
    stream, _, _ = make_market(FakeConnector())
    await stream.subscribe([f"K{i}" for i in range(2000)], "full")
    with pytest.raises(SubscriptionLimitError):
        await stream.subscribe(["one-more"], "full")
    assert "one-more" not in stream.subscriptions
    # Mixing modes switches to the (lower) combined limits: full -> 1500.
    with pytest.raises(SubscriptionLimitError, match="1500"):
        await stream.subscribe(["IDX"], "ltpc")


async def test_only_one_stream_of_each_kind() -> None:
    cf = FakeConnector()
    a, _, _ = make_market(cf)
    b, _, _ = make_market(cf)
    task = asyncio.create_task(a.run())
    await cf.wait_for_connections(1)
    with pytest.raises(ConnectionLimitError):
        await b.run()
    # A portfolio stream is a different kind and is allowed alongside.
    p = PortfolioStream(authorize=lambda: FakeAuthorizer().authorize_portfolio_feed(["order"]), connector=cf,
                        settings=FAST)
    ptask = asyncio.create_task(p.run())
    await cf.wait_for_connections(2)
    for s in (a, p):
        await s.stop()
    await asyncio.gather(task, ptask)


# --------------------------------------------------------------------------- #
# Portfolio stream
# --------------------------------------------------------------------------- #


async def test_portfolio_order_updates_parsed() -> None:
    cf = FakeConnector()
    updates: list[OrderUpdate] = []
    positions: list[dict[str, Any]] = []
    p = PortfolioStream(authorize=lambda: FakeAuthorizer().authorize_portfolio_feed(["order"]), connector=cf,
                        settings=FAST,
                        on_order_update=updates.append, on_position_update=positions.append)
    task = asyncio.create_task(p.run())
    conn = await cf.wait_for_connections(1)
    conn.push(json.dumps({
        "update_type": "order", "instrument_key": "NSE_FO|1", "transaction_type": "BUY", "quantity": 75,
        "filled_quantity": 25, "average_price": 120.5, "status": "open", "order_id": "2611030001",
        "tag": "LIVE-orb_options-abc", "order_timestamp": "2026-11-03 09:31:02", "status_message": "",
    }))
    conn.push(json.dumps({"update_type": "position", "instrument_token": "NSE_FO|1", "quantity": 25}))
    conn.push(json.dumps({"update_type": "holding"}))
    await until(lambda: updates and positions)
    u = updates[0]
    assert u.status is OrderStatus.PARTIALLY_FILLED
    assert u.broker_order_id == "2611030001" and u.client_order_id == "LIVE-orb_options-abc"
    await p.stop()
    await task
