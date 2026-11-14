"""The read-only market-data surface research code is given.

``ResearchSource`` names the only four calls research may make. The
composition root (``research.__main__``) wraps the Upstox REST client in
``ReadOnlySource``, so the object research modules hold has no order,
account or portfolio method at all, not even one that would refuse.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from core.models import Candle, Instrument


class ResearchSource(Protocol):
    async def get_daily_candles(self, instrument_key: str, from_date: date, to_date: date) -> list[Candle]: ...

    async def get_historical_candles(self, instrument_key: str, interval_minutes: int, from_date: date,
                                     to_date: date) -> list[Candle]: ...

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]: ...

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]: ...


class ReadOnlySource:
    """Forwards exactly the four ``ResearchSource`` calls; nothing else."""

    __slots__ = ("_delegate",)

    def __init__(self, delegate: ResearchSource) -> None:
        self._delegate = delegate

    async def get_daily_candles(self, instrument_key: str, from_date: date, to_date: date) -> list[Candle]:
        return await self._delegate.get_daily_candles(instrument_key, from_date, to_date)

    async def get_historical_candles(self, instrument_key: str, interval_minutes: int, from_date: date,
                                     to_date: date) -> list[Candle]:
        return await self._delegate.get_historical_candles(instrument_key, interval_minutes, from_date, to_date)

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        return await self._delegate.get_option_contracts(underlying_key, expiry)

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        return await self._delegate.get_option_chain(underlying_key, expiry)


__all__ = ["ReadOnlySource", "ResearchSource"]
