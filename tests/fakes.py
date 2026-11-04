"""Test doubles: fake clock, fake WebSocket connections, Upstox payload builders.

No test ever opens a real network connection.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from broker.proto import MarketDataFeedV3_pb2 as pb
from core.clock import IST

T0 = datetime(2026, 11, 3, 9, 15, tzinfo=IST)


class FakeClock:
    """Monotonic clock where sleep() advances time instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


class FakeWallClock:
    """Mutable IST wall clock for ``now_fn`` injection."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeConnection:
    def __init__(self, *, pong: bool = True) -> None:
        self.sent: list[str | bytes] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.pong = pong
        self.pings = 0

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("send on closed connection")
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]

    async def ping(self) -> asyncio.Future[None]:
        self.pings += 1
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self.pong:
            fut.set_result(None)
        return fut

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(ConnectionError("closed"))

    def push(self, message: str | bytes) -> None:
        self.inbox.put_nowait(message)

    def drop(self) -> None:
        """Simulate the server dropping the connection."""
        self.inbox.put_nowait(ConnectionError("connection dropped by server"))


class FakeConnector:
    """Hands out FakeConnections; ``fail_first`` connect attempts raise."""

    def __init__(self, *, fail_first: int = 0, pong: bool = True) -> None:
        self.fail_first = fail_first
        self.pong = pong
        self.attempts = 0
        self.urls: list[str] = []
        self.connections: list[FakeConnection] = []
        self.new_connection = asyncio.Event()

    async def __call__(self, url: str) -> FakeConnection:
        self.attempts += 1
        self.urls.append(url)
        if self.attempts <= self.fail_first:
            raise OSError("connection refused")
        conn = FakeConnection(pong=self.pong)
        self.connections.append(conn)
        self.new_connection.set()
        return conn

    async def wait_for_connections(self, n: int, timeout: float = 5.0) -> FakeConnection:
        async def _wait() -> None:
            while len(self.connections) < n:
                self.new_connection.clear()
                await self.new_connection.wait()

        await asyncio.wait_for(_wait(), timeout)
        return self.connections[n - 1]


class FakeAuthorizer:
    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.calls = 0
        self.fail_with = fail_with

    async def authorize_market_feed(self) -> str:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return f"wss://feed.example/market?code=one-time-{self.calls}"

    async def authorize_portfolio_feed(self, update_types: Any) -> str:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return f"wss://feed.example/portfolio?code=one-time-{self.calls}"


# --------------------------------------------------------------------------- #
# Protobuf market-feed builders
# --------------------------------------------------------------------------- #


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def feed_option(
    key: str,
    ltp: float,
    at: datetime,
    *,
    bid: float | None = None,
    ask: float | None = None,
    bid_qty: int = 1500,
    ask_qty: int = 1500,
    vtt: int = 0,
    oi: float = 0.0,
    msg_type: int = pb.live_feed,
) -> bytes:
    r = pb.FeedResponse(type=msg_type, currentTs=ms(at))
    m = r.feeds[key].fullFeed.marketFF
    m.ltpc.ltp = ltp
    m.ltpc.ltt = ms(at)
    m.ltpc.ltq = 75
    m.ltpc.cp = ltp - 1
    q = m.marketLevel.bidAskQuote.add()
    q.bidP = bid if bid is not None else ltp - 0.5
    q.askP = ask if ask is not None else ltp + 0.5
    q.bidQ = bid_qty
    q.askQ = ask_qty
    m.vtt = vtt
    m.oi = oi
    m.iv = 0.14
    m.optionGreeks.delta = 0.52
    r.feeds[key].requestMode = pb.full_d5
    return r.SerializeToString()


def feed_index(key: str, ltp: float, at: datetime, msg_type: int = pb.live_feed) -> bytes:
    r = pb.FeedResponse(type=msg_type, currentTs=ms(at))
    r.feeds[key].fullFeed.indexFF.ltpc.ltp = ltp
    r.feeds[key].fullFeed.indexFF.ltpc.ltt = ms(at)
    return r.SerializeToString()


def feed_ltpc(key: str, ltp: float, at: datetime) -> bytes:
    r = pb.FeedResponse(type=pb.live_feed, currentTs=ms(at))
    r.feeds[key].ltpc.ltp = ltp
    r.feeds[key].ltpc.ltt = ms(at)
    return r.SerializeToString()


def feed_market_info(status: dict[str, int]) -> bytes:
    r = pb.FeedResponse(type=pb.market_info)
    for seg, st in status.items():
        r.marketInfo.segmentStatus[seg] = st
    return r.SerializeToString()


# --------------------------------------------------------------------------- #
# REST payloads (shapes from the official Upstox docs)
# --------------------------------------------------------------------------- #


def option_contract(strike: float, opt: str, expiry: str = "2026-11-10", key: str | None = None,
                    lot: int = 75) -> dict[str, Any]:
    """SYNTHETIC instrument-list record. ``lot`` defaults to 75 for the
    original fixtures; production code never assumes it (see the lot-size
    tests, which use the real current 65 and an arbitrary value)."""
    return {
        "name": "NIFTY",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "expiry": expiry,
        "instrument_key": key or f"NSE_FO|{int(strike)}{opt}",
        "exchange_token": "1",
        "trading_symbol": f"NIFTY {int(strike)} {opt} 10 NOV 26",
        "tick_size": 5.0,
        "lot_size": lot,
        "instrument_type": opt,
        "freeze_quantity": float(lot * 24),
        "underlying_key": "NSE_INDEX|Nifty 50",
        "underlying_type": "INDEX",
        "underlying_symbol": "NIFTY",
        "strike_price": strike,
        "minimum_lot": lot,
        "weekly": True,
    }


def future_contract(expiry: str, key: str, lot: int = 75) -> dict[str, Any]:
    """SYNTHETIC NIFTY futures record in the Upstox instrument-list shape."""
    return {
        "weekly": False, "segment": "NSE_FO", "name": "NIFTY", "exchange": "NSE", "expiry": expiry,
        "instrument_type": "FUT", "underlying_symbol": "NIFTY", "instrument_key": key, "lot_size": lot,
        "freeze_quantity": float(lot * 27), "exchange_token": key.split("|")[-1], "minimum_lot": lot,
        "underlying_key": "NSE_INDEX|Nifty 50", "tick_size": 10.0, "underlying_type": "INDEX",
        "trading_symbol": f"NIFTY FUT {expiry}", "strike_price": 0.0,
    }


def chain_row(strike: float, spot: float, expiry: str = "2026-11-10", **md: Any) -> dict[str, Any]:
    def side(opt: str, ltp: float) -> dict[str, Any]:
        base = {"ltp": ltp, "volume": 250000, "oi": 1200000, "close_price": ltp, "bid_price": ltp - 0.5,
                "bid_qty": 1500, "ask_price": ltp + 0.5, "ask_qty": 1500, "prev_oi": 1100000}
        base.update(md.get(opt, {}))
        return {"instrument_key": f"NSE_FO|{int(strike)}{opt}", "market_data": base,
                "option_greeks": {"vega": 10.0, "theta": -12.0, "gamma": 0.001, "delta": 0.5, "iv": 13.5,
                                  "pop": 45.0}}

    intrinsic_c = max(0.0, spot - strike)
    intrinsic_p = max(0.0, strike - spot)
    return {"expiry": expiry, "pcr": 1.0, "strike_price": strike, "underlying_key": "NSE_INDEX|Nifty 50",
            "underlying_spot_price": spot, "call_options": side("CE", intrinsic_c + 100),
            "put_options": side("PE", intrinsic_p + 100)}
