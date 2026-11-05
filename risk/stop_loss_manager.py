"""Initial stop-loss / take-profit levels and exit-trigger checks (long options).

Invariants:
* Every trade has a stop-loss; it can never be removed.
* A stop can only move UP (tighten) for a long position — any attempt to
  widen it is refused and reported.
* Levels are rounded to the tick: stop rounded DOWN is avoided (that would
  widen risk) — stops round UP, targets round UP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from config.settings import StopLossConfig, TakeProfitConfig


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    STRUCTURAL_STOP = "STRUCTURAL_STOP"
    SQUARE_OFF = "SQUARE_OFF"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"
    STRATEGY = "STRATEGY"
    TRADE_MANAGER = "TRADE_MANAGER"  # the RL trade manager asked for a full exit (Phase 8)


class StopWideningError(ValueError):
    """Attempt to move a long position's stop further from price."""


@dataclass(frozen=True)
class ProtectiveLevels:
    entry: float
    stop: float
    target: float | None
    risk_per_unit: float  # entry - initial stop ("1R")


def _ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def compute_levels(
    entry: float, sl: StopLossConfig, tp: TakeProfitConfig, tick_size: float = 0.05
) -> ProtectiveLevels:
    if entry <= 0:
        raise ValueError("entry must be positive")
    raw_stop = entry * (1 - sl.value / 100) if sl.mode == "PREMIUM_PERCENT" else entry - sl.value
    stop = _ceil_tick(raw_stop, tick_size)
    if stop <= 0 or stop >= entry:
        raise ValueError(f"invalid stop {stop} for entry {entry}")
    r = entry - stop
    if tp.mode == "R_MULTIPLE":
        raw_target = entry + tp.value * r
    elif tp.mode == "PREMIUM_PERCENT":
        raw_target = entry * (1 + tp.value / 100)
    else:
        raw_target = entry + tp.value
    return ProtectiveLevels(entry=entry, stop=stop, target=_ceil_tick(raw_target, tick_size), risk_per_unit=r)


def tighten_stop(current: float, proposed: float) -> float:
    """Return the new stop for a long position; refuses to widen."""
    if proposed < current - 1e-9:
        raise StopWideningError(f"refusing to widen stop from {current} to {proposed}")
    return max(current, proposed)


def check_exit(ltp: float, stop: float, target: float | None) -> ExitReason | None:
    """Exit trigger for a long option on the latest traded price."""
    if ltp <= stop:
        return ExitReason.STOP_LOSS
    if target is not None and ltp >= target:
        return ExitReason.TARGET
    return None


def r_multiple(entry: float, exit_price: float, risk_per_unit: float) -> float | None:
    return None if risk_per_unit <= 0 else round((exit_price - entry) / risk_per_unit, 3)
