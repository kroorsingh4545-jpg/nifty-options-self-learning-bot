"""Upstox WebSocket adapters: market-data feed V3 and portfolio stream.

Lifecycle of a ``ResilientStream``::

    IDLE -> CONNECTING -> CONNECTED --(drop / heartbeat timeout)--> RECONNECTING
         -> CONNECTED ...            (exponential backoff + jitter between tries)
    auth rejected            -> AUTH_FAILED (stops; never loops on bad auth)
    max attempts exceeded    -> FAILED      (stops; Phase 3 kill-switch trigger)
    stop()                   -> STOPPED

* The authorized ``wss://`` URL is single-use, so it is re-requested (through
  the rate-limited REST client) on every connect.
* Heartbeat: a ping is sent every ``heartbeat_interval``; no pong within
  ``heartbeat_timeout`` drops the connection and triggers a reconnect.
* At most ONE market-data and ONE portfolio stream may run per process
  (Upstox allows 2 connections per user on the normal plan).
* ``MarketDataStream`` remembers every subscription and re-subscribes all of
  them after each reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from enum import StrEnum
from typing import Any

from broker.broker_interface import StreamConnection
from broker.errors import AuthError, ConnectionLimitError
from broker.upstox_mapping import decode_feed_response, parse_order_update
from broker.upstox_rate_limiter import Clock, RealClock, backoff_delay
from config.settings import MarketDataMode, WebSocketSection
from core.models import OrderUpdate, Tick
from monitoring.json_logger import get_logger

log = get_logger(__name__)

Authorizer = Callable[[], Awaitable[str]]
Connector = Callable[[str], Awaitable[StreamConnection]]

# Instrument-key subscription limits per mode (Upstox normal plan).
INDIVIDUAL_LIMITS: dict[str, int] = {"ltpc": 5000, "option_greeks": 3000, "full": 2000, "full_d30": 50}
COMBINED_LIMITS: dict[str, int] = {"ltpc": 2000, "option_greeks": 2000, "full": 1500, "full_d30": 50}


class StreamState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    AUTH_FAILED = "AUTH_FAILED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class HeartbeatTimeout(ConnectionError):
    pass


_ACTIVE_KINDS: set[str] = set()


async def default_connector(url: str) -> StreamConnection:  # pragma: no cover - real network
    from websockets.asyncio.client import connect

    # Our own heartbeat replaces the library ping loop; Upstox pings are auto-ponged.
    conn = await connect(url, ping_interval=None, open_timeout=10, max_size=2**22)
    return conn


class ResilientStream:
    kind = "stream"

    def __init__(
        self,
        *,
        authorize: Authorizer,
        connector: Connector = default_connector,
        settings: WebSocketSection | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._authorize = authorize
        self._connector = connector
        self.settings = settings or WebSocketSection()
        self._clock: Clock = clock or RealClock()
        self._rng = rng or random.Random()  # noqa: S311 - jitter only
        self.state = StreamState.IDLE
        self._conn: StreamConnection | None = None
        self._stopped = False
        self.reconnect_attempts = 0
        self.connect_count = 0
        self.messages_received = 0
        self.message_errors = 0  # messages that failed to decode / handle
        self.last_message_at: float | None = None
        self.last_error: str | None = None
        self._state_listeners: list[Callable[[str, StreamState, StreamState], None]] = []
        self._connected_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    def add_state_listener(self, fn: Callable[[str, StreamState, StreamState], None]) -> None:
        self._state_listeners.append(fn)

    def _set_state(self, new: StreamState) -> None:
        old, self.state = self.state, new
        if new is StreamState.CONNECTED:
            self._connected_event.set()
        else:
            self._connected_event.clear()
        if old is not new:
            log.info("websocket state change", extra={"stream": self.kind, "from": old.value, "to": new.value})
            for fn in list(self._state_listeners):
                try:
                    fn(self.kind, old, new)
                except Exception:  # a listener must never break the stream
                    log.exception("websocket state listener failed", extra={"stream": self.kind})

    @property
    def connected(self) -> bool:
        return self.state is StreamState.CONNECTED

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def is_stale(self) -> bool:
        """Connected but silent for longer than ``stale_after_seconds``."""
        if not self.connected or self.last_message_at is None:
            return self.connected
        return self._clock.monotonic() - self.last_message_at > self.settings.stale_after_seconds

    def last_message_age(self) -> float | None:
        """Seconds since the last message (or since connect), None before the first connect."""
        if self.last_message_at is None:
            return None
        return max(0.0, self._clock.monotonic() - self.last_message_at)

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        if self.kind in _ACTIVE_KINDS:
            raise ConnectionLimitError(f"a {self.kind} WebSocket is already running in this process")
        _ACTIVE_KINDS.add(self.kind)
        try:
            await self._run()
        finally:
            _ACTIVE_KINDS.discard(self.kind)

    async def _run(self) -> None:
        first = True
        while not self._stopped:
            self._set_state(StreamState.CONNECTING if first else StreamState.RECONNECTING)
            first = False
            try:
                url = await self._authorize()
                conn = await self._connector(url)
            except AuthError as exc:
                self.last_error = str(exc)
                self._set_state(StreamState.AUTH_FAILED)
                return
            except Exception as exc:
                self.last_error = f"connect failed: {type(exc).__name__}"
                if not await self._wait_before_reconnect():
                    return
                continue

            self._conn = conn
            self.connect_count += 1
            self.reconnect_attempts = 0
            self.last_message_at = self._clock.monotonic()
            self._set_state(StreamState.CONNECTED)
            try:
                await self.on_connected(conn)
                await self._serve(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("websocket connection lost", extra={"stream": self.kind, "error": self.last_error})
            finally:
                self._conn = None
                with contextlib.suppress(Exception):  # closing a dead socket may fail
                    await conn.close()
            if self._stopped:
                break
            self.on_disconnected()
            if not await self._wait_before_reconnect():
                return
        self._set_state(StreamState.STOPPED)

    async def _wait_before_reconnect(self) -> bool:
        """Back off before the next attempt. Returns False when giving up."""
        if self._stopped:
            self._set_state(StreamState.STOPPED)
            return False
        if self.reconnect_attempts >= self.settings.max_reconnect_attempts:
            log.error("websocket reconnect attempts exhausted", extra={"stream": self.kind})
            self._set_state(StreamState.FAILED)
            return False
        self._set_state(StreamState.RECONNECTING)
        delay = backoff_delay(
            self.reconnect_attempts, self.settings.reconnect_base_seconds, self.settings.reconnect_max_seconds,
            rng=self._rng,
        )
        self.reconnect_attempts += 1
        await self._clock.sleep(delay)
        return not self._stopped

    async def _serve(self, conn: StreamConnection) -> None:
        reader = asyncio.create_task(self._read_loop(conn))
        heart = asyncio.create_task(self._heartbeat_loop(conn))
        try:
            done, _ = await asyncio.wait({reader, heart}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise the failure that ended the connection
        finally:
            for task in (reader, heart):
                task.cancel()
            await asyncio.gather(reader, heart, return_exceptions=True)

    async def _read_loop(self, conn: StreamConnection) -> None:
        while True:
            message = await conn.recv()
            self.messages_received += 1
            self.last_message_at = self._clock.monotonic()
            try:
                await self.handle_message(message)
            except Exception:
                self.message_errors += 1
                log.exception("failed to handle websocket message",
                              extra={"stream": self.kind, "message_errors": self.message_errors})

    async def _heartbeat_loop(self, conn: StreamConnection) -> None:
        while True:
            await self._clock.sleep(self.settings.heartbeat_interval_seconds)
            waiter = await conn.ping()
            try:
                await asyncio.wait_for(asyncio.ensure_future(waiter), self.settings.heartbeat_timeout_seconds)
            except TimeoutError:
                raise HeartbeatTimeout(f"no pong within {self.settings.heartbeat_timeout_seconds}s") from None

    async def stop(self) -> None:
        self._stopped = True
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    # Hooks ---------------------------------------------------------------
    async def on_connected(self, conn: StreamConnection) -> None:
        """Called after every successful (re)connect."""

    def on_disconnected(self) -> None:
        """Called after a connection drops (before reconnecting)."""

    async def handle_message(self, message: str | bytes) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Market data feed V3
# --------------------------------------------------------------------------- #


class SubscriptionLimitError(ValueError):
    pass


class MarketDataStream(ResilientStream):
    kind = "market_data"

    def __init__(self, *, on_ticks: Callable[[list[Tick]], None] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.subscriptions: dict[str, str] = {}
        self.market_status: dict[str, str] = {}
        self._tick_listeners: list[Callable[[list[Tick]], None]] = [on_ticks] if on_ticks else []
        self._reconnect_listeners: list[Callable[[], None]] = []
        self.sent_requests: int = 0
        self.ticks_received: int = 0
        self._first_tick_pending = True

    def add_tick_listener(self, fn: Callable[[list[Tick]], None]) -> None:
        self._tick_listeners.append(fn)

    def add_reconnect_listener(self, fn: Callable[[], None]) -> None:
        """Called after a re-subscription that follows a reconnect."""
        self._reconnect_listeners.append(fn)

    # Subscription management ---------------------------------------------
    @staticmethod
    def check_limits(subs: dict[str, str]) -> None:
        counts: dict[str, int] = defaultdict(int)
        for mode in subs.values():
            counts[mode] += 1
        limits = COMBINED_LIMITS if len(counts) > 1 else INDIVIDUAL_LIMITS
        for mode, n in counts.items():
            if n > limits[mode]:
                raise SubscriptionLimitError(f"{n} '{mode}' subscriptions exceeds the Upstox limit of {limits[mode]}")

    async def subscribe(self, instrument_keys: Iterable[str], mode: MarketDataMode | None = None) -> None:
        mode = mode or self.settings.market_data_mode
        keys = [k for k in dict.fromkeys(instrument_keys) if self.subscriptions.get(k) != mode]
        if not keys:
            return
        proposed = dict(self.subscriptions)
        changed = [k for k in keys if k in proposed]
        new = [k for k in keys if k not in proposed]
        for k in keys:
            proposed[k] = mode
        self.check_limits(proposed)
        self.subscriptions = proposed
        if self._conn is not None:
            if new:
                await self._send(self._conn, "sub", new, mode)
            if changed:
                await self._send(self._conn, "change_mode", changed, mode)

    async def unsubscribe(self, instrument_keys: Iterable[str]) -> None:
        keys = [k for k in dict.fromkeys(instrument_keys) if k in self.subscriptions]
        if not keys:
            return
        for k in keys:
            self.subscriptions.pop(k)
        if self._conn is not None:
            await self._send(self._conn, "unsub", keys, None)

    async def _send(self, conn: StreamConnection, method: str, keys: Sequence[str], mode: str | None) -> None:
        size = self.settings.subscribe_chunk_size
        frames = 0
        for i in range(0, len(keys), size):
            data: dict[str, Any] = {"instrumentKeys": list(keys[i : i + size])}
            if mode is not None:
                data["mode"] = mode
            payload = {"guid": uuid.uuid4().hex, "method": method, "data": data}
            # Upstox requires subscription requests as *binary* frames.
            await conn.send(json.dumps(payload).encode("utf-8"))
            self.sent_requests += 1
            frames += 1
        log.info("market data subscription request sent",
                 extra={"stream": self.kind, "method": method, "feed_mode": mode, "instruments": len(keys),
                        "frames": frames, "total_subscriptions": len(self.subscriptions)})

    async def on_connected(self, conn: StreamConnection) -> None:
        self._first_tick_pending = True
        by_mode: dict[str, list[str]] = defaultdict(list)
        for key, mode in self.subscriptions.items():
            by_mode[mode].append(key)
        for mode, keys in by_mode.items():
            await self._send(conn, "sub", keys, mode)
        if self.connect_count > 1:
            log.info("market data re-subscribed", extra={"instruments": len(self.subscriptions)})
            for fn in list(self._reconnect_listeners):
                fn()

    async def handle_message(self, message: str | bytes) -> None:
        if isinstance(message, str):
            log.debug("ignoring text frame on market feed")
            return
        msg_type, ticks, segments = decode_feed_response(message)
        if segments:
            changed = {seg: st for seg, st in segments.items() if self.market_status.get(seg) != st}
            self.market_status.update(segments)
            if changed:
                log.info("market status update", extra={"stream": self.kind, "segments": changed})
        if ticks:
            self.ticks_received += len(ticks)
            if self._first_tick_pending:
                self._first_tick_pending = False
                log.info("first market data ticks after connect",
                         extra={"stream": self.kind, "message_type": msg_type, "instruments": len(ticks),
                                "connect_count": self.connect_count})
            for fn in list(self._tick_listeners):
                fn(ticks)


# --------------------------------------------------------------------------- #
# Portfolio stream (orders / positions)
# --------------------------------------------------------------------------- #


class PortfolioStream(ResilientStream):
    kind = "portfolio"

    def __init__(
        self,
        *,
        on_order_update: Callable[[OrderUpdate], None] | None = None,
        on_position_update: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._order_listeners = [on_order_update] if on_order_update else []
        self._position_listeners = [on_position_update] if on_position_update else []

    def add_order_listener(self, fn: Callable[[OrderUpdate], None]) -> None:
        self._order_listeners.append(fn)

    async def handle_message(self, message: str | bytes) -> None:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        rec = json.loads(text)
        kind = rec.get("update_type")
        if kind == "order":
            update = parse_order_update(rec)
            for fn in list(self._order_listeners):
                fn(update)
        elif kind == "position":
            for pfn in list(self._position_listeners):
                pfn(rec)
