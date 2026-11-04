"""Strategy 2 — VWAP + EMA momentum, traded with NIFTY options.

Signal instrument: NIFTY *futures* (near month) by default, because the NIFTY
index has no traded volume and VWAP needs volume. The runtime injects its
instrument key.

On each completed 3- or 5-minute candle (``timeframe_minutes``):

CE when, for the last ``momentum_bars`` candles,
    close > session VWAP  and  close > EMA(``ema_period``)  and closes rising,
    and the EMA slope is positive.
PE is the mirror image (below both, closes falling, EMA slope negative).

Filters (no signal when):
* the EMA is not warmed up (``ema_period`` candles; ``warm_up`` can seed it
  from earlier candles),
* the candle's volume < ``min_volume_ratio`` x the rolling average volume of
  the last ``chop_lookback_bars`` candles (low volume),
* the price crossed VWAP more than ``max_vwap_crosses`` times in the last
  ``chop_lookback_bars`` candles (choppy).

Signals fire on the transition into the condition and re-arm only after it
breaks, so a trend does not produce a signal on every candle.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.enums import OptionType
from core.models import Candle, Signal
from strategies.base_strategy import BaseStrategy
from strategies.indicators import EMA, RollingMean, SessionVWAP


class VwapEmaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ema_period: int = Field(default=20, ge=2, le=200)
    momentum_confirmation: Literal["consecutive_closes"] = "consecutive_closes"
    momentum_bars: int = Field(default=2, ge=1, le=6)
    min_volume_ratio: float = Field(default=1.2, ge=0)
    chop_filter: Literal["vwap_cross_count"] = "vwap_cross_count"
    max_vwap_crosses: int = Field(default=4, ge=0)
    chop_lookback_bars: int = Field(default=12, ge=2, le=100)
    max_entries_per_direction_per_day: int = Field(default=2, ge=1, le=4)
    signal_instrument: Literal["NIFTY_INDEX", "NIFTY_FUT"] = "NIFTY_FUT"


class VwapEmaStrategy(BaseStrategy):
    params_model = VwapEmaParams

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.timeframe not in (3, 5):
            raise ValueError("VWAP+EMA strategy uses 3- or 5-minute candles")
        p: VwapEmaParams = self.params
        self.ema = EMA(p.ema_period)  # carries across days (needs warm-up history)

    def _reset_state(self, day: date) -> None:
        p: VwapEmaParams = self.params
        self.vwap = SessionVWAP()
        self.volume = RollingMean(p.chop_lookback_bars)
        self.sides: deque[int] = deque(maxlen=p.chop_lookback_bars + 1)  # +1 above, -1 below VWAP
        self.recent: deque[Candle] = deque(maxlen=p.momentum_bars + 1)
        self.armed = {OptionType.CE: True, OptionType.PE: True}
        self.last_reason: str | None = None

    def _on_instrument_change(self) -> None:
        # A new futures contract trades at a different basis: indicators built
        # on the old contract would mislead, so start them afresh.
        p: VwapEmaParams = self.params
        self.ema = EMA(p.ema_period)
        self._reset_state(self.day or self._now().date())

    def warm_up(self, candles: Iterable[Candle]) -> int:
        """Seed the EMA from earlier completed candles (e.g. previous session)."""
        n = 0
        for c in sorted(candles, key=lambda x: x.start):
            if c.timeframe_minutes == self.timeframe and c.complete:
                self.ema.update(c.close)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    def generate(self, candle: Candle) -> Sequence[Signal]:
        p: VwapEmaParams = self.params
        # Indicators are updated with the just-closed candle only (point-in-time).
        avg_vol_before = self.volume.mean
        vwap = self.vwap.update(candle.start.date(), candle.high, candle.low, candle.close, candle.volume)
        ema = self.ema.update(candle.close)
        self.volume.update(candle.volume)
        self.recent.append(candle)
        if vwap is not None:
            self.sides.append(1 if candle.close > vwap else -1)

        if vwap is None or not self.ema.ready or len(self.recent) < p.momentum_bars + 1:
            self.last_reason = "warming up"
            return ()
        crosses = sum(1 for a, b in zip(list(self.sides), list(self.sides)[1:], strict=False) if a != b)
        bars = list(self.recent)[-p.momentum_bars:]
        prev = list(self.recent)[-p.momentum_bars - 1]
        slope = self.ema.slope or 0.0
        bull = all(c.close > vwap and c.close > ema for c in bars) and slope > 0 and \
            all(b.close > a.close for a, b in zip([prev, *bars[:-1]], bars, strict=False))
        bear = all(c.close < vwap and c.close < ema for c in bars) and slope < 0 and \
            all(b.close < a.close for a, b in zip([prev, *bars[:-1]], bars, strict=False))
        if not bull:
            self.armed[OptionType.CE] = True
        if not bear:
            self.armed[OptionType.PE] = True
        if not (bull or bear):
            self.last_reason = "no momentum setup"
            return ()
        if not self.in_entry_window(candle.end):
            self.last_reason = "outside entry window"
            return ()
        if crosses > p.max_vwap_crosses:
            self.last_reason = f"choppy: {crosses} VWAP crosses"
            return ()
        vol_ratio = candle.volume / avg_vol_before if avg_vol_before else 0.0
        if vol_ratio < p.min_volume_ratio:
            self.last_reason = f"low volume ({vol_ratio:.2f}x)"
            return ()
        opt = OptionType.CE if bull else OptionType.PE
        if not self.armed[opt] or self.entries[opt] >= p.max_entries_per_direction_per_day:
            return ()
        self.armed[opt] = False
        feats = {"close": candle.close, "vwap": vwap, "ema": ema, "ema_slope": slope,
                 "vwap_dist_pct": (candle.close - vwap) / vwap * 100, "volume_ratio": vol_ratio,
                 "vwap_crosses": crosses}
        side = "above" if bull else "below"
        reason = (f"VWAP+EMA momentum: {p.momentum_bars} closes {side} VWAP {vwap:.2f} and EMA{p.ema_period} "
                  f"{ema:.2f}, volume {vol_ratio:.2f}x")
        self.last_reason = reason
        return (self.make_signal(opt, candle, reason, feats),)

    def _snapshot(self) -> dict[str, Any]:
        return {"vwap": self.vwap.value, "ema": self.ema.value, "ema_ready": self.ema.ready,
                "last_reason": self.last_reason}
