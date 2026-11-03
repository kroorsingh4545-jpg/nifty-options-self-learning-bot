"""Trailing stops for long option positions — they only ever tighten.

Modes (``TrailingConfig.mode``):

* ``PREMIUM_PERCENT`` — activate once profit >= ``activation`` % of entry;
  stop trails ``step`` % below the peak premium.
* ``POINTS`` — activate once profit >= ``activation`` points; stop trails
  ``step`` points below the peak.
* ``R_MULTIPLE`` — activate once profit >= ``activation`` R (R = initial risk
  per unit); stop trails ``step`` R below the peak. Once activated, the stop is
  never below breakeven (entry).
* ``STRUCTURAL`` — the exit level is a NIFTY (underlying) structure level
  supplied by the strategy (e.g. last swing low for CE, swing high for PE).
  It only tightens and triggers on the underlying price. The premium stop
  remains in force alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import TrailingConfig
from core.enums import OptionType
from risk.stop_loss_manager import _ceil_tick


@dataclass
class TrailingState:
    entry: float
    risk_per_unit: float
    stop: float
    peak: float
    active: bool = False
    underlying_stop: float | None = None  # STRUCTURAL mode


class TrailingStopManager:
    def __init__(self, cfg: TrailingConfig, option_type: OptionType, tick_size: float = 0.05) -> None:
        self.cfg = cfg
        self.option_type = option_type
        self.tick = tick_size

    def new_state(self, entry: float, stop: float) -> TrailingState:
        return TrailingState(entry=entry, risk_per_unit=entry - stop, stop=stop, peak=entry)

    def _profit_threshold(self, s: TrailingState) -> float:
        a = self.cfg.activation
        if self.cfg.mode == "PREMIUM_PERCENT":
            return s.entry * a / 100
        if self.cfg.mode == "POINTS":
            return a
        return a * s.risk_per_unit  # R_MULTIPLE

    def _trail_distance(self, s: TrailingState) -> float:
        st = self.cfg.step
        if self.cfg.mode == "PREMIUM_PERCENT":
            return s.peak * st / 100
        if self.cfg.mode == "POINTS":
            return st
        return st * s.risk_per_unit

    def update(self, s: TrailingState, ltp: float) -> bool:
        """Update with the latest premium. Returns True if the stop moved up."""
        if not self.cfg.enabled or self.cfg.mode == "STRUCTURAL":
            s.peak = max(s.peak, ltp)
            return False
        s.peak = max(s.peak, ltp)
        if not s.active and s.peak - s.entry >= self._profit_threshold(s) - 1e-9:
            s.active = True
        if not s.active:
            return False
        candidate = _ceil_tick(s.peak - self._trail_distance(s), self.tick)
        if self.cfg.mode == "R_MULTIPLE":
            candidate = max(candidate, s.entry)  # lock in at least breakeven
        candidate = min(candidate, _ceil_tick(ltp, self.tick) - self.tick)  # never at/above the market
        if candidate > s.stop + 1e-9:
            s.stop = round(candidate, 2)
            return True
        return False

    # STRUCTURAL ------------------------------------------------------------
    def update_structure(self, s: TrailingState, level: float) -> bool:
        """Offer a new underlying structure level; accepted only if tighter.
        CE (long calls): level may only rise. PE (long puts): only fall."""
        if not self.cfg.enabled or self.cfg.mode != "STRUCTURAL":
            return False
        cur = s.underlying_stop
        tighter = cur is None or (level > cur if self.option_type is OptionType.CE else level < cur)
        if tighter:
            s.underlying_stop = level
        return tighter

    def structural_breached(self, s: TrailingState, underlying: float) -> bool:
        if s.underlying_stop is None:
            return False
        if self.option_type is OptionType.CE:
            return underlying <= s.underlying_stop
        return underlying >= s.underlying_stop
