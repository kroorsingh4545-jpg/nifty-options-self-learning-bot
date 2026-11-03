"""Central rate limiter: windows, queueing, buffer, 429 circuit breaker,
backoff, debouncing and batching."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence

import pytest

from broker.errors import CircuitOpenError, RateLimitError
from broker.upstox_rate_limiter import (
    ApiCategory,
    KeyBatcher,
    RequestCoalescer,
    SlidingWindow,
    UpstoxRateLimiter,
    backoff_delay,
)
from config.loader import load_config
from config.settings import CircuitBreakerSection, EffectiveRateLimits, WindowLimits
from tests.fakes import FakeClock

EFFECTIVE = load_config(use_default_env_file=False).risk.rate_limits.effective


def make(clock: FakeClock, **kw: object) -> UpstoxRateLimiter:
    return UpstoxRateLimiter(EFFECTIVE, CircuitBreakerSection(), clock=clock, queue_timeout=10_000, **kw)  # type: ignore[arg-type]


def max_in_window(times: Sequence[float], window: float) -> int:
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


# --------------------------------------------------------------------------- #
# Limits with 20% buffer
# --------------------------------------------------------------------------- #


def test_effective_limits_are_80pct_of_official() -> None:
    assert EFFECTIVE.order_api == WindowLimits(per_second=8, per_minute=400, per_30_minutes=1600)
    assert EFFECTIVE.standard_api == WindowLimits(per_second=40, per_minute=400, per_30_minutes=1600)


def test_limiter_refuses_limits_above_buffer() -> None:
    too_high = EffectiveRateLimits(
        order_api=WindowLimits(per_second=10, per_minute=500, per_30_minutes=2000),
        standard_api=EFFECTIVE.standard_api,
    )
    with pytest.raises(ValueError, match="20% buffer"):
        UpstoxRateLimiter(too_high)


def test_sliding_window_basics() -> None:
    w = SlidingWindow(limit=2, window=1.0)
    assert w.wait_time(0.0) == 0
    w.record(0.0)
    w.record(0.1)
    assert w.wait_time(0.5) == pytest.approx(0.5)
    assert w.wait_time(1.0) == 0  # first event expired
    assert w.count(1.05) == 1


# --------------------------------------------------------------------------- #
# Enforcement of all three windows
# --------------------------------------------------------------------------- #


async def test_order_api_never_exceeds_per_second_and_per_minute() -> None:
    clock = FakeClock()
    lim = make(clock)
    times: list[float] = []
    for _ in range(900):
        await lim.acquire(ApiCategory.ORDER)
        times.append(clock.t)
    assert max_in_window(times, 1.0) <= 8
    assert max_in_window(times, 60.0) <= 400
    # 900 requests at 400/min need more than two minutes.
    assert times[-1] - times[0] >= 120


async def test_order_api_30_minute_window() -> None:
    clock = FakeClock()
    lim = make(clock)
    times: list[float] = []
    for _ in range(1601):
        await lim.acquire(ApiCategory.ORDER)
        times.append(clock.t)
    assert max_in_window(times, 1800.0) <= 1600
    assert times[1600] - times[0] >= 1800


async def test_standard_api_per_second_limit() -> None:
    clock = FakeClock()
    lim = make(clock)
    times = []
    for _ in range(200):
        await lim.acquire(ApiCategory.STANDARD)
        times.append(clock.t)
    assert max_in_window(times, 1.0) <= 40
    assert max_in_window(times, 60.0) <= 400


async def test_categories_are_independent() -> None:
    clock = FakeClock()
    lim = make(clock)
    for _ in range(8):
        await lim.acquire(ApiCategory.ORDER)
    t = clock.t
    for _ in range(40):
        await lim.acquire(ApiCategory.STANDARD)
    assert clock.t == t  # standard budget unaffected by order usage


async def test_concurrent_requests_queue_fifo_and_respect_limit() -> None:
    clock = FakeClock()
    lim = make(clock)
    order: list[int] = []
    stamps: list[float] = []

    async def worker(i: int) -> None:
        await lim.acquire(ApiCategory.ORDER)
        order.append(i)
        stamps.append(clock.t)

    await asyncio.gather(*(worker(i) for i in range(30)))
    assert order == list(range(30))
    assert max_in_window(sorted(stamps), 1.0) <= 8


async def test_request_rejected_not_sent_when_queue_timeout_exceeded() -> None:
    clock = FakeClock()
    lim = make(clock)
    for _ in range(8):
        await lim.acquire(ApiCategory.ORDER)
    with pytest.raises(RateLimitError):
        await lim.acquire(ApiCategory.ORDER, timeout=0.1)
    assert lim.usage()["order_api"]["rejected"] == 1
    assert lim.usage()["order_api"]["windows"]["per_second"]["used"] == 8


async def test_usage_snapshot() -> None:
    clock = FakeClock()
    lim = make(clock)
    for _ in range(4):
        await lim.acquire(ApiCategory.ORDER)
    u = lim.usage()["order_api"]
    assert u["windows"]["per_second"] == {"used": 4, "limit": 8, "pct": 50.0}
    assert u["windows"]["per_30_minutes"]["limit"] == 1600
    assert u["circuit_open"] is False


# --------------------------------------------------------------------------- #
# HTTP 429 circuit breaker
# --------------------------------------------------------------------------- #


async def test_429_opens_circuit_with_exponential_cooldown() -> None:
    clock = FakeClock()
    lim = make(clock)
    assert lim.record_429(ApiCategory.STANDARD) == 1.0
    assert lim.record_429(ApiCategory.STANDARD) == 2.0
    assert lim.record_429(ApiCategory.STANDARD) == 4.0
    start = clock.t
    await lim.acquire(ApiCategory.STANDARD)
    assert clock.t - start >= 4.0 - 1e-9  # waited out the cooldown before sending


async def test_retry_after_header_is_respected() -> None:
    clock = FakeClock()
    lim = make(clock)
    assert lim.record_429(ApiCategory.ORDER, retry_after=7.5) == 7.5
    start = clock.t
    await lim.acquire(ApiCategory.ORDER)
    assert clock.t - start >= 7.5


async def test_success_resets_consecutive_count() -> None:
    clock = FakeClock()
    lim = make(clock)
    lim.record_429(ApiCategory.STANDARD)
    lim.record_429(ApiCategory.STANDARD)
    lim.record_success(ApiCategory.STANDARD)
    assert lim.record_429(ApiCategory.STANDARD) == 1.0


async def test_circuit_open_beyond_timeout_raises_circuit_error() -> None:
    clock = FakeClock()
    lim = make(clock)
    lim.record_429(ApiCategory.STANDARD, retry_after=60)
    with pytest.raises(CircuitOpenError):
        await lim.acquire(ApiCategory.STANDARD, timeout=1)


async def test_repeated_429_locks_circuit_and_fires_trip_hook() -> None:
    clock = FakeClock()
    tripped: list[ApiCategory] = []
    lim = make(clock, on_circuit_trip=tripped.append)
    for _ in range(5):
        lim.record_429(ApiCategory.ORDER)
    assert tripped == [ApiCategory.ORDER]
    clock.t += 10_000  # even after any cooldown the circuit stays locked
    with pytest.raises(CircuitOpenError, match="locked"):
        await lim.acquire(ApiCategory.ORDER)
    # Other category still works.
    await lim.acquire(ApiCategory.STANDARD)
    lim.reset_circuit(ApiCategory.ORDER)
    await lim.acquire(ApiCategory.ORDER)


def test_cooldown_is_capped() -> None:
    clock = FakeClock()
    lim = UpstoxRateLimiter(EFFECTIVE, CircuitBreakerSection(max_consecutive_429=100), clock=clock)
    cooldowns = [lim.record_429(ApiCategory.STANDARD) for _ in range(12)]
    assert max(cooldowns) == 60.0


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def test_backoff_grows_exponentially_and_caps() -> None:
    raw = [backoff_delay(a, 0.5, 30, jitter=False) for a in range(8)]
    assert raw[:4] == [0.5, 1.0, 2.0, 4.0]
    assert raw[-1] == 30


def test_backoff_jitter_bounds() -> None:
    rng = random.Random(42)  # noqa: S311
    for a in range(6):
        cap = min(30, 0.5 * 2**a)
        for _ in range(50):
            d = backoff_delay(a, 0.5, 30, rng=rng)
            assert cap / 2 <= d <= cap


# --------------------------------------------------------------------------- #
# Debounce / batching
# --------------------------------------------------------------------------- #


async def test_coalescer_shares_inflight_and_caches_within_window() -> None:
    clock = FakeClock()
    c = RequestCoalescer(window=1.0, clock=clock)
    calls = 0
    gate = asyncio.Event()

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return 42

    t1 = asyncio.create_task(c.run("k", fetch))
    t2 = asyncio.create_task(c.run("k", fetch))
    await asyncio.sleep(0)
    gate.set()
    assert await t1 == 42 and await t2 == 42
    assert calls == 1
    assert await c.run("k", fetch) == 42  # cached
    assert calls == 1
    clock.t += 2
    assert await c.run("k", fetch) == 42
    assert calls == 2


async def test_coalescer_propagates_errors_and_does_not_cache_them() -> None:
    c = RequestCoalescer(window=10.0, clock=FakeClock())
    attempts = 0

    async def boom() -> int:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        await c.run("k", boom)
    with pytest.raises(RuntimeError):
        await c.run("k", boom)
    assert attempts == 2


async def test_batcher_combines_requests_into_chunks() -> None:
    clock = FakeClock()
    seen: list[list[str]] = []

    async def fetch(keys: Sequence[str]) -> dict[str, float]:
        seen.append(list(keys))
        return {k: float(i) for i, k in enumerate(keys)}

    b: KeyBatcher[float] = KeyBatcher(fetch, max_batch=3, window=0.05, clock=clock)
    # Duplicate K0 arrives while K0 is still pending -> fetched once.
    results = await asyncio.gather(b.get("K0"), b.get("K0"), *(b.get(f"K{i}") for i in range(1, 7)))
    assert all(r is not None for r in results)
    assert sum(len(s) for s in seen) == 7
    assert all(len(s) <= 3 for s in seen)


async def test_batcher_propagates_failure_to_all_waiters() -> None:
    async def fetch(keys: Sequence[str]) -> dict[str, float]:
        raise RuntimeError("down")

    b: KeyBatcher[float] = KeyBatcher(fetch, max_batch=10, window=0.01, clock=FakeClock())
    res = await asyncio.gather(b.get("A"), b.get("B"), return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in res)
