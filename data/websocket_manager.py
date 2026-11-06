"""Owns the bot's WebSocket connections.

* Exactly one market-data stream (always) and at most one portfolio stream
  (LIVE only — PAPER orders never exist at Upstox, so no portfolio socket is
  opened in PAPER mode).
* Wires ticks into ``MarketDataService`` and marks data stale after every
  reconnect until fresh ticks arrive.
* Exposes a health snapshot and status listeners (Phase 3 wires these into
  the kill switch: disconnect / FAILED / AUTH_FAILED).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol

from broker.upstox_rate_limiter import Clock
from broker.upstox_websocket import (
    Connector,
    MarketDataStream,
    PortfolioStream,
    StreamState,
    default_connector,
)
from config.settings import MarketDataMode, WebSocketSection
from core.models import OrderUpdate
from data.market_data import MarketDataService


class FeedAuthorizer(Protocol):
    async def authorize_market_feed(self) -> str: ...

    async def authorize_portfolio_feed(self, update_types: Sequence[str]) -> str: ...


class WebSocketManager:
    def __init__(
        self,
        *,
        authorizer: FeedAuthorizer,
        market_data: MarketDataService,
        settings: WebSocketSection | None = None,
        enable_portfolio: bool = False,
        connector: Connector = default_connector,
        clock: Clock | None = None,
        on_order_update: Callable[[OrderUpdate], None] | None = None,
    ) -> None:
        self.settings = settings or WebSocketSection()
        self.market_data = market_data
        self.market = MarketDataStream(
            authorize=authorizer.authorize_market_feed,
            connector=connector,
            settings=self.settings,
            clock=clock,
            on_ticks=market_data.on_ticks,
        )
        self.market.add_reconnect_listener(market_data.mark_all_stale)
        self.portfolio: PortfolioStream | None = None
        if enable_portfolio:
            types = self.settings.portfolio_update_types
            self.portfolio = PortfolioStream(
                authorize=lambda: authorizer.authorize_portfolio_feed(types),
                connector=connector,
                settings=self.settings,
                clock=clock,
                on_order_update=on_order_update,
            )
        self._tasks: list[asyncio.Task[None]] = []
        self._status_listeners: list[Callable[[str, StreamState, StreamState], None]] = []
        for stream in self.streams():
            stream.add_state_listener(self._on_state)

    def streams(self) -> list[MarketDataStream | PortfolioStream]:
        return [s for s in (self.market, self.portfolio) if s is not None]

    def add_status_listener(self, fn: Callable[[str, StreamState, StreamState], None]) -> None:
        self._status_listeners.append(fn)

    def _on_state(self, kind: str, old: StreamState, new: StreamState) -> None:
        if kind == "market_data" and old is StreamState.CONNECTED and new is not StreamState.CONNECTED:
            self.market_data.mark_all_stale()
        for fn in list(self._status_listeners):
            fn(kind, old, new)

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("WebSocketManager already started")
        for stream in self.streams():
            self._tasks.append(asyncio.create_task(stream.run(), name=f"ws-{stream.kind}"))

    async def stop(self) -> None:
        for stream in self.streams():
            await stream.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def subscribe(self, instrument_keys: Iterable[str], mode: MarketDataMode | None = None) -> None:
        await self.market.subscribe(instrument_keys, mode)

    async def unsubscribe(self, instrument_keys: Iterable[str]) -> None:
        await self.market.unsubscribe(instrument_keys)

    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in self.streams():
            out[s.kind] = {
                "state": s.state.value,
                "connected": s.connected,
                "stale": s.is_stale(),
                "reconnect_attempts": s.reconnect_attempts,
                "connect_count": s.connect_count,
                "messages": s.messages_received,
                "last_error": s.last_error,
            }
        out["market_data"]["subscriptions"] = len(self.market.subscriptions)
        out["market_data"]["market_status"] = dict(self.market.market_status)
        return out
