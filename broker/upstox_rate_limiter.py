"""Central Upstox rate limiter — every REST call to Upstox goes through here.

* Two categories with independent budgets: ``ORDER`` (place/modify/cancel)
  and ``STANDARD`` (everything else, including WebSocket authorize calls).
* Each category enforces three sliding windows simultaneously (1 s, 60 s,
  30 min) at the *effective* limits = official limits minus the safety buffer
  (validated in ``config``; re-checked here).
* Requests queue FIFO per category. A request that cannot get a slot within
  its timeout raises ``RateLimitError`` — it is never sent.
* HTTP 429 opens a per-category circuit breaker with exponential cooldown
  (respecting ``Retry-After``). Too many consecutive 429s lock the circuit
  until a manual reset and fire ``on_circuit_trip`` (kill-switch hook).
* Helpers: ``backoff_delay`` (exponential + jitter), ``RequestCoalescer``
  (debounce identical GETs), ``KeyBatcher`` (batch per-instrument lookups).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar

from broker.errors import CircuitOpenError, RateLimitError
from config import hard_limits
from config.settings import BotConfig, CircuitBreakerSection, EffectiveRateLimits, WindowLimits

T = TypeVar("T")

WINDOWS: tuple[tuple[str, float], ...] = (("per_second", 1.0), ("per_minute", 60.0), ("per_30_minutes", 1800.0))


class ApiCategory(StrEnum):
    ORDER = "order_api"
    STANDARD = "standard_api"


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def backoff_delay(
    attempt: int, base: float, cap: float, *, jitter: bool = True, rng: random.Random | None = None
) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random.

    attempt 0 -> ~base, attempt 1 -> ~2*base, ... capped at ``cap``.
    """
    raw = float(min(cap, base * 2.0 ** max(0, attempt)))
    if not jitter:
        return raw
    r = rng or random  # noqa: S311 - jitter, not cryptography
    return raw / 2 + float(r.uniform(0, raw / 2))


# --------------------------------------------------------------------------- #
# Windows and circuit breaker
# --------------------------------------------------------------------------- #


class SlidingWindow:
    """Counts events in the trailing ``window`` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit
        self.window = window
        self._events: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def count(self, now: float) -> int:
        self._prune(now)
        return len(self._events)

    def wait_time(self, now: float) -> float:
        """Seconds until one more event fits (0 if it fits now)."""
        self._prune(now)
        if len(self._events) < self.limit:
            return 0.0
        return self._events[len(self._events) - self.limit] + self.window - now

    def record(self, now: float) -> None:
        self._events.append(now)


class CircuitBreaker:
    def __init__(
        self,
        category: ApiCategory,
        cfg: CircuitBreakerSection,
        clock: Clock,
        on_trip: Callable[[ApiCategory], None] | None = None,
    ) -> None:
        self.category = category
        self.cfg = cfg
        self.clock = clock
        self.on_trip = on_trip
        self.consecutive_429 = 0
        self.total_429 = 0
        self.open_until = 0.0
        self.locked = False

    def record_429(self, retry_after: float | None = None) -> float:
        """Register an HTTP 429. Returns the cooldown applied (seconds)."""
        self.consecutive_429 += 1
        self.total_429 += 1
        cooldown = float(
            min(self.cfg.max_cooldown_seconds, self.cfg.base_cooldown_seconds * 2.0 ** (self.consecutive_429 - 1))
        )
        if retry_after is not None and retry_after > 0:
            cooldown = max(cooldown, retry_after)
        self.open_until = max(self.open_until, self.clock.monotonic() + cooldown)
        if self.consecutive_429 >= self.cfg.max_consecutive_429 and not self.locked:
            self.locked = True
            if self.on_trip is not None:
                self.on_trip(self.category)
        return cooldown

    def record_success(self) -> None:
        self.consecutive_429 = 0

    def remaining(self, now: float) -> float:
        return max(0.0, self.open_until - now)

    def is_open(self) -> bool:
        return self.locked or self.remaining(self.clock.monotonic()) > 0

    def reset(self) -> None:
        """Manual reset (operator action) after a hard trip."""
        self.locked = False
        self.consecutive_429 = 0
        self.open_until = 0.0


@dataclass(frozen=True)
class WindowUsage:
    used: int
    limit: int

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.limit


class CategoryLimiter:
    def __init__(self, category: ApiCategory, limits: WindowLimits, breaker: CircuitBreaker, clock: Clock) -> None:
        self.category = category
        self.clock = clock
        self.breaker = breaker
        self.windows = {name: SlidingWindow(getattr(limits, name), secs) for name, secs in WINDOWS}
        self._lock = asyncio.Lock()  # FIFO queue of waiters
        self.waiting = 0
        self.granted = 0
        self.rejected = 0

    async def acquire(self, timeout: float) -> None:
        deadline = self.clock.monotonic() + timeout
        self.waiting += 1
        try:
            async with self._lock:
                while True:
                    now = self.clock.monotonic()
                    if self.breaker.locked:
                        self.rejected += 1
                        raise CircuitOpenError(f"{self.category.value} circuit is locked after repeated HTTP 429")
                    breaker_wait = self.breaker.remaining(now)
                    window_wait = max(w.wait_time(now) for w in self.windows.values())
                    wait = max(breaker_wait, window_wait)
                    if wait <= 0:
                        for w in self.windows.values():
                            w.record(now)
                        self.granted += 1
                        return
                    if now + wait > deadline:
                        self.rejected += 1
                        if breaker_wait >= window_wait:
                            raise CircuitOpenError(
                                f"{self.category.value} circuit open for {breaker_wait:.1f}s (HTTP 429 cooldown)"
                            )
                        raise RateLimitError(
                            f"{self.category.value}: no rate-limit slot within {timeout:.1f}s (needs {wait:.2f}s)"
                        )
                    await self.clock.sleep(wait)
        finally:
            self.waiting -= 1

    def usage(self) -> dict[str, Any]:
        now = self.clock.monotonic()
        return {
            "windows": {
                name: {"used": w.count(now), "limit": w.limit, "pct": round(100 * w.count(now) / w.limit, 1)}
                for name, w in self.windows.items()
            },
            "queued": self.waiting,
            "granted": self.granted,
            "rejected": self.rejected,
            "circuit_open": self.breaker.is_open(),
            "circuit_locked": self.breaker.locked,
            "total_429": self.breaker.total_429,
        }


def _assert_within_buffer(limits: EffectiveRateLimits) -> None:
    factor = 1.0 - hard_limits.MIN_RATE_LIMIT_SAFETY_BUFFER
    for cat in ApiCategory:
        configured: WindowLimits = getattr(limits, cat.value)
        official = hard_limits.UPSTOX_OFFICIAL_LIMITS[cat.value]
        for window, ceiling in official.items():
            if getattr(configured, window) > ceiling * factor + 1e-9:
                raise ValueError(f"{cat.value}.{window} is above official limit minus the 20% buffer")


class UpstoxRateLimiter:
    """The single, process-wide gate for Upstox REST requests."""

    def __init__(
        self,
        limits: EffectiveRateLimits,
        breaker_cfg: CircuitBreakerSection | None = None,
        *,
        clock: Clock | None = None,
        queue_timeout: float = 30.0,
        on_circuit_trip: Callable[[ApiCategory], None] | None = None,
    ) -> None:
        _assert_within_buffer(limits)
        self.clock: Clock = clock or RealClock()
        self.queue_timeout = queue_timeout
        cfg = breaker_cfg or CircuitBreakerSection()
        self._categories = {
            cat: CategoryLimiter(
                cat, getattr(limits, cat.value), CircuitBreaker(cat, cfg, self.clock, on_circuit_trip), self.clock
            )
            for cat in ApiCategory
        }

    @classmethod
    def from_config(
        cls,
        cfg: BotConfig,
        *,
        clock: Clock | None = None,
        on_circuit_trip: Callable[[ApiCategory], None] | None = None,
    ) -> UpstoxRateLimiter:
        return cls(
            cfg.risk.rate_limits.effective,
            cfg.settings.broker.circuit_breaker,
            clock=clock,
            queue_timeout=cfg.settings.broker.queue_timeout_seconds,
            on_circuit_trip=on_circuit_trip,
        )

    def category(self, category: ApiCategory) -> CategoryLimiter:
        return self._categories[ApiCategory(category)]

    async def acquire(self, category: ApiCategory, timeout: float | None = None) -> None:
        await self.category(category).acquire(self.queue_timeout if timeout is None else timeout)

    def record_429(self, category: ApiCategory, retry_after: float | None = None) -> float:
        return self.category(category).breaker.record_429(retry_after)

    def record_success(self, category: ApiCategory) -> None:
        self.category(category).breaker.record_success()

    def reset_circuit(self, category: ApiCategory) -> None:
        self.category(category).breaker.reset()

    def usage(self) -> dict[str, Any]:
        """Usage snapshot for the dashboard / monitoring."""
        return {cat.value: lim.usage() for cat, lim in self._categories.items()}


# --------------------------------------------------------------------------- #
# Debouncing and batching
# --------------------------------------------------------------------------- #


def _consume_exception(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()


class RequestCoalescer:
    """Debounce identical requests.

    Concurrent calls with the same key share one in-flight request; a result
    is reused for ``window`` seconds afterwards. Use only for idempotent reads.
    """

    def __init__(self, window: float, clock: Clock | None = None) -> None:
        self.window = window
        self.clock: Clock = clock or RealClock()
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}
        self._cache: dict[Hashable, tuple[float, Any]] = {}
        self.calls = 0
        self.hits = 0

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        cached = self._cache.get(key)
        if cached is not None and self.clock.monotonic() - cached[0] <= self.window:
            self.hits += 1
            return cached[1]  # type: ignore[no-any-return]
        if key in self._inflight:
            self.hits += 1
            return await asyncio.shield(self._inflight[key])
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        fut.add_done_callback(_consume_exception)
        self._inflight[key] = fut
        self.calls += 1
        try:
            result = await factory()
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        else:
            self._cache[key] = (self.clock.monotonic(), result)
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    def invalidate(self, key: Hashable | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)


class KeyBatcher(Generic[T]):
    """Collect per-key lookups for ``window`` seconds and resolve them with
    batched calls of at most ``max_batch`` keys (e.g. LTP for 500 keys)."""

    def __init__(
        self,
        fetch: Callable[[Sequence[str]], Awaitable[dict[str, T]]],
        *,
        max_batch: int,
        window: float,
        clock: Clock | None = None,
    ) -> None:
        self.fetch = fetch
        self.max_batch = max_batch
        self.window = window
        self.clock: Clock = clock or RealClock()
        self._pending: dict[str, asyncio.Future[T | None]] = {}
        self._timer: asyncio.Task[None] | None = None
        self.batches = 0

    async def get(self, key: str) -> T | None:
        fut = self._pending.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            fut.add_done_callback(_consume_exception)
            self._pending[key] = fut
            if len(self._pending) >= self.max_batch:
                await self._flush()
            elif self._timer is None:
                self._timer = asyncio.create_task(self._delayed_flush())
        return await fut

    async def _delayed_flush(self) -> None:
        await self.clock.sleep(self.window)
        self._timer = None
        await self._flush()

    async def _flush(self) -> None:
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
            self._timer = None
        pending, self._pending = self._pending, {}
        keys = list(pending)
        for i in range(0, len(keys), self.max_batch):
            chunk = keys[i : i + self.max_batch]
            self.batches += 1
            try:
                result = await self.fetch(chunk)
            except Exception as exc:  # resolve every waiter with the failure
                for k in chunk:
                    if not pending[k].done():
                        pending[k].set_exception(exc)
                continue
            for k in chunk:
                if not pending[k].done():
                    pending[k].set_result(result.get(k))
