"""The ORB strategy's signal rule on one historical session (pure).

Research may not import ``strategies.orb_options`` (isolation), so this
restates its rule; ``tests/unit/test_regime_orb_parity.py`` drives the real
strategy with the same candles and requires identical signals.

The rule (``strategies/orb_options.py``): the opening range is the high / low
of the 1-minute bars from ``range_start`` to ``range_end`` (every minute must be
present; width at least ``min_range_points`` and at most ``max_range_pct`` %).
On each completed ``timeframe``-minute candle inside the entry window (start at
or after ``window_start``, end before ``window_end``): a close above high +
buffer fires CE, below low - buffer fires PE, only when that direction is armed;
a close back inside the range re-arms both. History assumes every signal was
approved, so each direction fires at most ``max_entries`` times a day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class OrbRule:
    range_start: time = time(9, 15)
    range_end: time = time(9, 30)
    window_start: time = time(9, 30)
    window_end: time = time(11, 0)
    buffer_points: float = 5.0
    min_range_points: float = 10.0
    max_range_pct: float = 1.5
    max_entries: int = 1
    timeframe: int = 5
    session_open: time = time(9, 15)


@dataclass(frozen=True)
class OrbSignal:
    candle_end: datetime
    option_type: str  # CE | PE
    close: float
    or_high: float
    or_low: float


def _t(day: date, t: time, tz: object) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)  # type: ignore[arg-type]


def orb_signals(day: date, bars: Mapping[time, tuple[float, float, float, float]], rule: OrbRule,
                tz: object) -> list[OrbSignal]:
    """``bars``: {1-minute bar START time: (open, high, low, close)} of one session."""
    expected = int((_t(day, rule.range_end, tz) - _t(day, rule.range_start, tz)).total_seconds() // 60)
    rng = [bars[t] for t in sorted(bars) if rule.range_start <= t and
           (datetime.combine(day, t) + timedelta(minutes=1)).time() <= rule.range_end]
    if len(rng) < expected:
        return []
    hi, lo = max(b[1] for b in rng), min(b[2] for b in rng)
    width = hi - lo
    if width < rule.min_range_points or width / lo * 100 > rule.max_range_pct:
        return []
    buckets: dict[time, list[tuple[time, float]]] = {}
    origin = _t(day, rule.session_open, tz)
    for t, b in bars.items():
        start = _t(day, t, tz)
        k = int((start - origin).total_seconds() // 60) // rule.timeframe
        bucket = (origin + timedelta(minutes=k * rule.timeframe)).timetz()
        buckets.setdefault(bucket.replace(tzinfo=None), []).append((t, b[3]))
    armed = {"CE": True, "PE": True}
    entries = {"CE": 0, "PE": 0}
    out: list[OrbSignal] = []
    for bucket in sorted(buckets):
        start = _t(day, bucket, tz)
        end = start + timedelta(minutes=rule.timeframe)
        if start.time() < rule.window_start or end.time() >= rule.window_end:
            continue
        close = max(buckets[bucket])[1]  # the close of the bucket's last minute
        if lo <= close <= hi:
            armed = {"CE": True, "PE": True}
            continue
        opt = "CE" if close > hi + rule.buffer_points else ("PE" if close < lo - rule.buffer_points else None)
        if opt is None or not armed[opt] or entries[opt] >= rule.max_entries:
            continue
        armed[opt] = False
        entries[opt] += 1
        out.append(OrbSignal(end, opt, close, hi, lo))
    return out


__all__ = ["OrbRule", "OrbSignal", "orb_signals"]
