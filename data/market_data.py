"""Market-data service: latest-tick cache, staleness tracking, fan-out.

The WebSocket is the primary source. REST snapshots (``recover_snapshot``)
are debounced and used only for recovery; snapshot data never counts as
fresh for trading decisions.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from statistics import median
from typing import Any

from broker.broker_interface import MarketDataSource
from broker.upstox_rate_limiter import RequestCoalescer
from core.clock import now_ist
from core.models import Tick
from monitoring.json_logger import get_logger

log = get_logger(__name__)

#: live ticks per instrument kept for the feed-lag median
LAG_SAMPLES = 30


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
        self._listeners: list[Callable[[Tick], object]] = []
        self._coalescer = RequestCoalescer(snapshot_debounce_seconds)
        self.ticks_received = 0
        self.out_of_order = 0
        # instrument -> recent (local receipt - exchange time) in seconds, live ticks only
        self._lag: dict[str, deque[float]] = {}
        # instrument -> (last live price, when that price first arrived)
        self._price_since: dict[str, tuple[float, datetime]] = {}

    def add_listener(self, fn: Callable[[Tick], object]) -> None:
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
            lag = (tick.received_at - tick.exchange_time).total_seconds()
            self._lag.setdefault(tick.instrument_key, deque(maxlen=LAG_SAMPLES)).append(lag)
            since = self._price_since.get(tick.instrument_key)
            if since is None or since[0] != tick.ltp:
                self._price_since[tick.instrument_key] = (tick.ltp, tick.received_at)
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

    def feed_lag(self, instrument_key: str) -> float | None:
        """Median of (local receipt - exchange time) over the instrument's recent
        live ticks, in seconds. For an actively traded instrument (the NIFTY
        index) this is network latency plus the local clock's offset; for an
        illiquid one the exchange time is its last trade, so the lag means
        nothing. None before any live tick."""
        samples = self._lag.get(instrument_key)
        return float(median(samples)) if samples else None

    def frozen_seconds(self, instrument_key: str) -> float | None:
        """How long fresh live ticks have kept arriving at one unchanged price.
        None while the instrument has no fresh live tick: a silent feed is the
        staleness check's job, this catches a feed that ticks but never moves
        (which the lag figure cannot see)."""
        since = self._price_since.get(instrument_key)
        if since is None or not self.is_fresh(instrument_key):
            return None
        return max(0.0, (self._now() - since[1]).total_seconds())

    def quote_status(self, instrument_key: str) -> dict[str, Any]:
        """Latest price of one instrument for display: ltp, freshness, age."""
        t = self._latest.get(instrument_key)
        if t is None:
            return {"ltp": None, "fresh": False, "age_seconds": None, "exchange_time": None, "snapshot": False}
        return {"ltp": t.ltp, "fresh": self.is_fresh(instrument_key),
                "age_seconds": round(max(0.0, (self._now() - t.received_at).total_seconds()), 1),
                "exchange_time": t.exchange_time.isoformat(), "snapshot": t.is_snapshot}

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
