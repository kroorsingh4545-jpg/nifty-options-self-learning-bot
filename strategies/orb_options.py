"""Strategy 1 — Opening Range Breakout on NIFTY, traded with options.

1. Opening range = high/low of NIFTY from 09:15 to 09:30 IST, built from the
   completed 1-minute candles of that window (all must be inside it).
2. Confirmation: a ``timeframe_minutes`` candle (default 5) CLOSES beyond the
   level plus ``breakout_buffer_points``:
      close > range_high + buffer  -> CE (bullish)
      close < range_low  - buffer  -> PE (bearish)
3. Only on the *transition* from inside to outside; it re-arms only after a
   close back inside the range (no repeated signals on every candle).
4. Entry window 09:30–11:00 (config); at most
   ``max_entries_per_direction_per_day`` approved entries per direction.
5. Optional sanity filter: the range must be at least ``min_range_points`` and
   at most ``max_range_pct`` % of price, and every minute of it must be present
   (a range with missing minutes could understate the real high / low).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.enums import OptionType
from core.models import Candle, Signal
from strategies.base_strategy import BaseStrategy


def _hhmm(v: Any) -> Any:
    if isinstance(v, str):
        h, m = v.split(":")
        return time(int(h), int(m))
    return v


class OrbParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening_range_start: time = time(9, 15)
    opening_range_end: time = time(9, 30)
    breakout_confirmation: Literal["candle_close_beyond_level"] = "candle_close_beyond_level"
    breakout_buffer_points: float = Field(default=5.0, ge=0)
    max_entries_per_direction_per_day: int = Field(default=1, ge=1, le=4)
    min_range_points: float = Field(default=10.0, ge=0)
    max_range_pct: float = Field(default=1.5, gt=0)
    signal_instrument: Literal["NIFTY_INDEX", "NIFTY_FUT"] = "NIFTY_INDEX"

    _parse = field_validator("opening_range_start", "opening_range_end", mode="before")(_hhmm)


class OrbOptionsStrategy(BaseStrategy):
    params_model = OrbParams

    def timeframes_used(self) -> set[int]:
        return {1, self.timeframe}

    def _reset_state(self, day: date) -> None:
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_minutes = 0
        self.range_ready = False
        self.range_invalid: str | None = None
        self.armed = {OptionType.CE: True, OptionType.PE: True}
        self.last_reason = None
        self.last_context = {}

    # ------------------------------------------------------------------ #
    def _build_range(self, c: Candle) -> None:
        p: OrbParams = self.params
        if c.timeframe_minutes != 1 or self.range_ready:
            return
        if p.opening_range_start <= c.start.time() and c.end.time() <= p.opening_range_end:
            self.or_high = c.high if self.or_high is None else max(self.or_high, c.high)
            self.or_low = c.low if self.or_low is None else min(self.or_low, c.low)
            self.or_minutes += 1
        if c.end.time() >= p.opening_range_end and self.or_high is not None and self.or_low is not None:
            self.range_ready = True
            width = self.or_high - self.or_low
            day = date(2000, 1, 1)
            expected = int((datetime.combine(day, p.opening_range_end)
                            - datetime.combine(day, p.opening_range_start)).total_seconds() // 60)
            if self.or_minutes < expected:
                self.range_invalid = f"range incomplete: {self.or_minutes} of {expected} minutes"
            elif width < p.min_range_points:
                self.range_invalid = f"range {width:.1f} < {p.min_range_points} points"
            elif width / self.or_low * 100 > p.max_range_pct:
                self.range_invalid = f"range {width / self.or_low * 100:.2f}% > {p.max_range_pct}%"

    def generate(self, candle: Candle) -> Sequence[Signal]:
        self._build_range(candle)
        if candle.timeframe_minutes != self.timeframe:
            return ()
        self.last_context = {"close": candle.close, "or_high": self.or_high, "or_low": self.or_low,
                             "or_minutes": self.or_minutes}
        if not self.range_ready:
            self.last_reason = "opening range not built"
            return ()
        if self.range_invalid:
            self.last_reason = f"opening range invalid: {self.range_invalid}"
            return ()
        w = self.config.entry_window
        # The confirming candle must lie inside the window and close before it ends.
        if candle.start.time() < w.start or candle.end.time() >= w.end:
            self.last_reason = "outside entry window"
            return ()
        p: OrbParams = self.params
        assert self.or_high is not None and self.or_low is not None
        up, down = self.or_high + p.breakout_buffer_points, self.or_low - p.breakout_buffer_points
        inside = self.or_low <= candle.close <= self.or_high
        if inside:  # re-arm both directions
            self.armed = {OptionType.CE: True, OptionType.PE: True}
            self.last_reason = "close inside the opening range"
            return ()
        width = self.or_high - self.or_low
        feats = {"or_high": self.or_high, "or_low": self.or_low, "or_width": width, "close": candle.close}
        if candle.close > up:
            return self._fire(OptionType.CE, candle, f"ORB breakout: close {candle.close:.2f} > high "
                              f"{self.or_high:.2f} + {p.breakout_buffer_points}", feats)
        if candle.close < down:
            return self._fire(OptionType.PE, candle, f"ORB breakdown: close {candle.close:.2f} < low "
                              f"{self.or_low:.2f} - {p.breakout_buffer_points}", feats)
        self.last_reason = "outside the range but within the breakout buffer"
        return ()

    def _fire(self, opt: OptionType, candle: Candle, reason: str, feats: dict[str, float]) -> Sequence[Signal]:
        p: OrbParams = self.params
        if self.replaying:  # a breakout that already happened is not a fresh one
            self.armed[opt] = False
            self.last_reason = "history replay"
            return ()
        if not self.armed[opt]:
            self.last_reason = "waiting for a fresh breakout"
            return ()
        if self.entries[opt] >= p.max_entries_per_direction_per_day:
            self.last_reason = f"max {opt.value} entries reached today"
            return ()
        self.armed[opt] = False
        self.last_reason = reason
        level = feats["or_high"] if opt is OptionType.CE else feats["or_low"]
        feats = {**feats, "breakout_in_ranges": abs(candle.close - level) / (feats["or_width"] or 1.0)}
        return (self.make_signal(opt, candle, reason, feats),)

    def _snapshot(self) -> dict[str, Any]:
        return {"opening_range": {"high": self.or_high, "low": self.or_low, "ready": self.range_ready,
                                  "invalid": self.range_invalid, "minutes": self.or_minutes}}
