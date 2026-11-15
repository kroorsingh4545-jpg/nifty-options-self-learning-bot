"""The bot's trade rules, as plain values, for counterfactual simulation.

Research may not import ``risk/``, ``execution/`` or the bot configuration
(isolation), so this module re-states the few pure rules the simulator needs:

* initial stop / target from the entry reference price
  (``risk.stop_loss_manager.compute_levels``)
* the exit trigger on the last traded price (``check_exit``)
* trailing stops that only tighten (``risk.trailing_stop_manager``)

``tests/unit/test_shadow_rules_parity.py`` runs these against the bot's own
functions on the same inputs, so the two cannot drift apart unnoticed. The
composition root (``research/__main__.py``) builds ``TradeRules`` from the bot
configuration; nothing here reads it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time

#: exit reasons, the same strings the bot records
STOP_LOSS, TARGET, TRAILING_STOP, SQUARE_OFF, END_OF_DATA = (
    "STOP_LOSS", "TARGET", "TRAILING_STOP", "SQUARE_OFF", "END_OF_DATA")


@dataclass(frozen=True)
class TradeRules:
    """Everything one strategy's counterfactual trade needs (values from the bot config)."""

    stop_mode: str  # PREMIUM_PERCENT | POINTS
    stop_value: float
    target_mode: str  # R_MULTIPLE | PREMIUM_PERCENT | POINTS
    target_value: float
    trailing_enabled: bool
    trailing_mode: str  # PREMIUM_PERCENT | POINTS | R_MULTIPLE | STRUCTURAL
    trailing_activation: float
    trailing_step: float
    strike_mode: str = "ATM"  # ATM | ITM | OTM
    strike_offset: int = 0
    strike_step: int = 50
    min_premium: float = 0.0
    max_premium: float | None = None
    min_volume: int = 0
    min_oi: float = 0.0
    max_spread_pct: float = 100.0
    tick_size: float = 0.05
    slippage_ticks: int = 1  # paper broker: adverse ticks on every fill
    entry_buffer_ticks: int = 2  # entry LIMIT = ask + buffer ticks
    entry_timeout_seconds: float = 30.0  # unfilled entry is cancelled
    max_quote_age_seconds: float = 5.0  # never fill against an older quote
    square_off_start: time = time(15, 10)

    def __post_init__(self) -> None:
        if self.stop_mode not in ("PREMIUM_PERCENT", "POINTS"):
            raise ValueError(f"unknown stop mode {self.stop_mode}")
        if self.target_mode not in ("R_MULTIPLE", "PREMIUM_PERCENT", "POINTS"):
            raise ValueError(f"unknown target mode {self.target_mode}")
        if self.trailing_mode not in ("PREMIUM_PERCENT", "POINTS", "R_MULTIPLE", "STRUCTURAL"):
            raise ValueError(f"unknown trailing mode {self.trailing_mode}")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")


def ceil_tick(x: float, tick: float) -> float:
    """``risk.stop_loss_manager._ceil_tick``."""
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 2)


@dataclass(frozen=True)
class Levels:
    entry: float
    stop: float
    target: float | None
    risk_per_unit: float


def levels(entry: float, r: TradeRules) -> Levels | None:
    """``compute_levels``; ``None`` where the bot would refuse (invalid stop)."""
    if entry <= 0:
        return None
    raw_stop = entry * (1 - r.stop_value / 100) if r.stop_mode == "PREMIUM_PERCENT" else entry - r.stop_value
    stop = ceil_tick(raw_stop, r.tick_size)
    if stop <= 0 or stop >= entry:
        return None
    risk = entry - stop
    if r.target_mode == "R_MULTIPLE":
        raw_target = entry + r.target_value * risk
    elif r.target_mode == "PREMIUM_PERCENT":
        raw_target = entry * (1 + r.target_value / 100)
    else:
        raw_target = entry + r.target_value
    return Levels(entry, stop, ceil_tick(raw_target, r.tick_size), risk)


def check_exit(ltp: float, stop: float, target: float | None) -> str | None:
    """``risk.stop_loss_manager.check_exit``."""
    if ltp <= stop:
        return STOP_LOSS
    if target is not None and ltp >= target:
        return TARGET
    return None


class Trail:
    """``TrailingStopManager`` + ``TrailingState`` for one long option. STRUCTURAL
    trailing (an underlying level supplied by the strategy) is not simulated:
    only the premium stop applies, which the caller flags."""

    def __init__(self, r: TradeRules, lv: Levels) -> None:
        self.r = r
        self.entry = lv.entry
        self.risk = lv.entry - lv.stop
        self.stop = lv.stop
        self.peak = lv.entry
        self.active = False

    def _threshold(self) -> float:
        a = self.r.trailing_activation
        if self.r.trailing_mode == "PREMIUM_PERCENT":
            return self.entry * a / 100
        if self.r.trailing_mode == "POINTS":
            return a
        return a * self.risk

    def _distance(self) -> float:
        s = self.r.trailing_step
        if self.r.trailing_mode == "PREMIUM_PERCENT":
            return self.peak * s / 100
        if self.r.trailing_mode == "POINTS":
            return s
        return s * self.risk

    def update(self, ltp: float) -> bool:
        """New last price; True when the stop moved up."""
        self.peak = max(self.peak, ltp)
        if not self.r.trailing_enabled or self.r.trailing_mode == "STRUCTURAL":
            return False
        if not self.active and self.peak - self.entry >= self._threshold() - 1e-9:
            self.active = True
        if not self.active:
            return False
        tick = self.r.tick_size
        candidate = ceil_tick(self.peak - self._distance(), tick)
        if self.r.trailing_mode == "R_MULTIPLE":
            candidate = max(candidate, self.entry)
        candidate = min(candidate, ceil_tick(ltp, tick) - tick)
        if candidate > self.stop + 1e-9:
            self.stop = round(candidate, 2)
            return True
        return False


__all__ = ["END_OF_DATA", "SQUARE_OFF", "STOP_LOSS", "TARGET", "TRAILING_STOP", "Levels", "Trail", "TradeRules",
           "ceil_tick", "check_exit", "floor_tick", "levels"]
