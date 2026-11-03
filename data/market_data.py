"""Market-data service: latest-tick cache, staleness tracking, fan-out.

The WebSocket is the primary source. REST snapshots (``recover_snapshot``)
are debounced and used only for recovery; snapshot data never counts as
fresh for trading decisions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime

from broker.broker_interface import MarketDataSource
from broker.upstox_rate_limiter import RequestCoalescer
from core.clock import now_ist
from core.models import Tick
from monitoring.json_logger import get_logger

log = get_logger(__name__)


class MarketDataService:
    def __init__(
        self,
        *,
        stale_after_seconds: float = 10.0,
        rest: MarketDataSource | None = None,
        now_fn: Callable[[], datetime] = now_ist,
        snapshot_debounce_seconds: float = 1.0,
    ) -> None:
        self.stale_after_seconds = stale_after_seconds
        self._rest = rest
        self._now = now_fn
        self._latest: dict[str, Tick] = {}
        self._stale: set[str] = set()
        self._listeners: list[Callable[[Tick], None]] = []
        self._coalescer = RequestCoalescer(snapshot_debounce_seconds)
        self.ticks_received = 0
        self.out_of_order = 0

    def add_listener(self, fn: Callable[[Tick], None]) -> None:
        self._listeners.append(fn)

    # Ingest --------------------------------------------------------------
    def on_ticks(self, ticks: Iterable[Tick]) -> None:
        for tick in ticks:
            self.on_tick(tick)

    def on_tick(self, tick: Tick) -> None:
        self.ticks_received += 1
        prev = self._latest.get(tick.instrument_key)
        if prev is not None and tick.exchange_time < prev.exchange_time and not prev.is_snapshot:
            self.out_of_order += 1
            return
        self._latest[tick.instrument_key] = tick
        if not tick.is_snapshot:
            self._stale.discard(tick.instrument_key)
        for fn in list(self._listeners):
            try:
                fn(tick)
            except Exception:
                log.exception("market data listener failed", extra={"instrument": tick.instrument_key})

    # Query ---------------------------------------------------------------
    def get(self, instrument_key: str) -> Tick | None:
        return self._latest.get(instrument_key)

    def ltp(self, instrument_key: str) -> float | None:
        t = self._latest.get(instrument_key)
        return t.ltp if t else None

    def is_fresh(self, instrument_key: str, max_age_seconds: float | None = None) -> bool:
        """True only for a live (non-snapshot) tick received recently and not
        invalidated by a reconnect."""
        t = self._latest.get(instrument_key)
        if t is None or t.is_snapshot or instrument_key in self._stale:
            return False
        limit = self.stale_after_seconds if max_age_seconds is None else max_age_seconds
        return (self._now() - t.received_at).total_seconds() <= limit

    def stale_instruments(self, keys: Iterable[str] | None = None) -> list[str]:
        candidates = list(keys) if keys is not None else list(self._latest)
        return [k for k in candidates if not self.is_fresh(k)]

    def mark_all_stale(self) -> None:
        """Invalidate every cached tick (e.g. after a reconnect) until a fresh
        live tick arrives for it."""
        self._stale = set(self._latest)
        log.warning("market data marked stale", extra={"instruments": len(self._stale)})

    # Recovery ------------------------------------------------------------
    async def recover_snapshot(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        """Debounced REST LTP snapshot. Results are stored as snapshot ticks and
        do NOT clear staleness."""
        if self._rest is None:
            return {}
        keys = tuple(sorted(set(instrument_keys)))
        rest = self._rest
        prices: dict[str, float] = await self._coalescer.run(("ltp", keys), lambda: rest.get_ltp(keys))
        now = self._now()
        for key, price in prices.items():
            prev = self._latest.get(key)
            if prev is None or prev.is_snapshot:
                self._latest[key] = Tick(instrument_key=key, ltp=price, exchange_time=now, received_at=now,
                                         is_snapshot=True)
        return prices
