"""Abstract broker interfaces.

Two layers:

* ``MarketDataSource`` — read-only REST access (instruments, quotes, option
  chain, candles). Used by ``data/`` in both PAPER and LIVE.
* ``Broker`` — account access (positions, funds, orders). In PAPER mode the
  injected implementation is ``PaperBroker``; only ``execution/`` may call its
  order methods.

Streaming interfaces (``StreamConnection``) let WebSocket code be tested with
fakes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Protocol

from core.enums import TradingMode
from core.models import BrokerPosition, Candle, Funds, Instrument, OrderAck, OrderRequest, OrderUpdate

OrderUpdateListener = Callable[[OrderUpdate], None]


class MarketDataSource(ABC):
    """Read-only market data over REST (snapshots and recovery only)."""

    @abstractmethod
    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]: ...

    @abstractmethod
    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]: ...

    @abstractmethod
    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]: ...

    @abstractmethod
    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]: ...


class Broker(ABC):
    """Account and order access. Implementations: PaperBroker (PAPER)."""

    mode: TradingMode

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_funds(self) -> Funds: ...

    @abstractmethod
    async def get_orders(self) -> list[OrderUpdate]: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderAck: ...

    @abstractmethod
    async def modify_order(
        self,
        client_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> OrderAck: ...

    @abstractmethod
    async def cancel_order(self, client_order_id: str) -> OrderAck: ...

    @abstractmethod
    def add_order_listener(self, listener: OrderUpdateListener) -> None: ...


class StreamConnection(Protocol):
    """Minimal WebSocket connection surface (satisfied by ``websockets``)."""

    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def ping(self) -> Any: ...

    async def close(self) -> None: ...


StreamConnector = Callable[[str], Any]  # async (url) -> StreamConnection
