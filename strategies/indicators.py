"""Incremental, point-in-time indicators (each update uses only data up to and
including the candle just closed — no lookahead)."""

from __future__ import annotations

from collections import deque
from datetime import date


class EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self.count = 0
        self.prev: float | None = None

    def update(self, x: float) -> float:
        self.prev = self.value
        self.count += 1
        # Seed with the SMA of the first `period` values, then exponential.
        if self.count <= self.period:
            self.value = x if self.value is None else self.value + (x - self.value) / self.count
        else:
            assert self.value is not None
            self.value = self.value + self.alpha * (x - self.value)
        return self.value

    @property
    def ready(self) -> bool:
        return self.count >= self.period

    @property
    def slope(self) -> float | None:
        return None if self.value is None or self.prev is None else self.value - self.prev


class SessionVWAP:
    """Volume-weighted average price of the typical price, reset every day."""

    def __init__(self) -> None:
        self.day: date | None = None
        self.pv = 0.0
        self.vol = 0.0

    def update(self, day: date, high: float, low: float, close: float, volume: float) -> float | None:
        if day != self.day:
            self.day, self.pv, self.vol = day, 0.0, 0.0
        if volume > 0:
            self.pv += (high + low + close) / 3 * volume
            self.vol += volume
        return self.value

    @property
    def value(self) -> float | None:
        return self.pv / self.vol if self.vol > 0 else None


class RollingMean:
    def __init__(self, n: int) -> None:
        self.values: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> None:
        self.values.append(x)

    @property
    def mean(self) -> float | None:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def full(self) -> bool:
        return len(self.values) == self.values.maxlen
