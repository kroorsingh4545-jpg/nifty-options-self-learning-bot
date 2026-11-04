"""Triple-barrier labels on the option premium.

For an entry decided at ``t0`` (a signal's candle end) on a long option:

* entry:          open of the option's 1-minute bar that STARTS at ``t0``
                  (the first price available after the decision), plus
                  adverse slippage. No such bar within ``max_entry_delay`` ->
                  no label (the event is dropped, never guessed).
* upper barrier:  the target (from the strategy's take-profit rule)
* lower barrier:  the stop-loss (from the strategy's stop-loss rule)
* time barrier:   ``t0 + max_hold_minutes``, capped at the square-off start
                  (positions are never held into the square-off window)

Bars are walked forward from the entry bar. Inside one bar the price path
follows the backtester's convention: an up-bar (close >= open) visits its low
before its high, a down-bar its high before its low, so when both barriers
are inside one up-bar the STOP is hit first. A bar that opens beyond a
barrier exits at the open (a gap through the stop fills worse than the stop).

The label is known at ``t1`` (the barrier touch, or the time barrier). The
interval ``[t0, t1]`` is what purged cross-validation uses to drop
overlapping training samples. Labels look into the future by design; they are
never features.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any

import numpy as np

from broker.charges import ChargeCalculator
from config.settings import StrategyConfig
from core.clock import IST, ensure_ist
from ml.feature_engineering import ANNUALISE_1M, Bars, RegimeThresholds


class BarrierHit(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    TIME = "TIME"


@dataclass(frozen=True)
class BarrierSpec:
    """Stop / target rules mirroring ``StrategyConfig.stop_loss`` / ``take_profit``."""

    stop_mode: str  # PREMIUM_PERCENT | POINTS
    stop_value: float
    target_mode: str  # R_MULTIPLE | PREMIUM_PERCENT | POINTS
    target_value: float
    max_hold_minutes: int = 60

    def __post_init__(self) -> None:
        if self.stop_mode not in ("PREMIUM_PERCENT", "POINTS") or self.stop_value <= 0:
            raise ValueError("stop must be PREMIUM_PERCENT or POINTS with a positive value")
        if self.stop_mode == "PREMIUM_PERCENT" and self.stop_value >= 100:
            raise ValueError("a PREMIUM_PERCENT stop must be below 100")
        if self.target_mode not in ("R_MULTIPLE", "PREMIUM_PERCENT", "POINTS") or self.target_value <= 0:
            raise ValueError("target must be R_MULTIPLE, PREMIUM_PERCENT or POINTS with a positive value")
        if self.max_hold_minutes < 1:
            raise ValueError("max_hold_minutes must be >= 1")

    @classmethod
    def from_strategy(cls, scfg: StrategyConfig, max_hold_minutes: int = 60) -> BarrierSpec:
        return cls(scfg.stop_loss.mode, scfg.stop_loss.value, scfg.take_profit.mode, scfg.take_profit.value,
                   max_hold_minutes)

    def levels(self, entry: float) -> tuple[float, float]:
        """(stop, target) for a long option bought at ``entry``."""
        stop = entry * (1 - self.stop_value / 100) if self.stop_mode == "PREMIUM_PERCENT" else entry - self.stop_value
        stop = max(stop, 0.05)
        risk = entry - stop
        if self.target_mode == "R_MULTIPLE":
            target = entry + self.target_value * risk
        elif self.target_mode == "PREMIUM_PERCENT":
            target = entry * (1 + self.target_value / 100)
        else:
            target = entry + self.target_value
        return stop, target


@dataclass(frozen=True)
class BarrierLabel:
    t0: datetime
    t1: datetime
    entry: float
    exit: float
    stop: float
    target: float
    hit: BarrierHit
    holding_minutes: float
    return_pct: float  # gross premium move, %
    r_multiple: float  # gross, in units of the initial risk
    net_pnl_per_lot: float  # rupees for one lot, after charges and slippage
    net_return_pct: float  # net_pnl_per_lot / (entry * lot_units) * 100
    charges_per_lot: float
    mfe_pct: float  # maximum favourable excursion before exit, %
    mae_pct: float  # maximum adverse excursion before exit, %

    @property
    def target_first(self) -> bool:
        return self.hit is BarrierHit.TARGET

    @property
    def loss(self) -> bool:
        return self.net_pnl_per_lot < 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hit"] = self.hit.value
        d["target_first"] = self.target_first
        d["loss"] = self.loss
        return d


def triple_barrier(option: Bars, t0: datetime, spec: BarrierSpec, *, lot_units: int, square_off_start: time,
                   charges: ChargeCalculator, slippage: float = 0.0, tick_size: float = 0.05,
                   max_entry_delay: timedelta = timedelta(minutes=1)) -> BarrierLabel | None:
    """Label one long-option entry at ``t0``. ``slippage`` is rupees per unit,
    applied adversely to both entry and exit."""
    if lot_units < 1:
        raise ValueError("lot_units must be >= 1 (from the instrument list)")
    t0 = ensure_ist(t0)
    ts0 = int(t0.timestamp())
    i = int(np.searchsorted(option.start, ts0, side="left"))
    if i >= len(option) or option.start[i] - ts0 > max_entry_delay.total_seconds():
        return None
    day = datetime.fromtimestamp(int(option.start[i]), IST).date()
    deadline = min(t0 + timedelta(minutes=spec.max_hold_minutes), datetime.combine(day, square_off_start, tzinfo=IST))
    ts_deadline = int(deadline.timestamp())
    if option.start[i] >= ts_deadline:
        return None  # no time left to hold the position
    entry = round(float(option.open[i]) + slippage, 2)
    if entry <= 0:
        return None
    stop, target = spec.levels(entry)
    hit, exit_px, j_exit = BarrierHit.TIME, float("nan"), i
    hi_seen, lo_seen = entry, entry
    j = i
    while j < len(option) and option.start[j] < ts_deadline and option.day[j] == option.day[i]:
        o, h, lo, c = (float(option.open[j]), float(option.high[j]), float(option.low[j]), float(option.close[j]))
        j_exit = j
        if j > i and o <= stop:
            hit, exit_px = BarrierHit.STOP, o
            break
        if j > i and o >= target:
            hit, exit_px = BarrierHit.TARGET, o
            break
        path = (lo, h) if c >= o else (h, lo)  # up-bar: low first; down-bar: high first
        done = False
        for p in path:
            hi_seen, lo_seen = max(hi_seen, p), min(lo_seen, p)
            if p <= stop:
                hit, exit_px, done = BarrierHit.STOP, stop, True
                break
            if p >= target:
                hit, exit_px, done = BarrierHit.TARGET, target, True
                break
        if done:
            break
        hi_seen, lo_seen = max(hi_seen, c), min(lo_seen, c)
        exit_px = c
        j += 1
    exit_px = max(tick_size, round(exit_px - slippage, 2))
    hi_seen, lo_seen = max(hi_seen, exit_px), min(lo_seen, exit_px)
    t1 = datetime.fromtimestamp(int(option.end[j_exit]), IST)
    if hit is not BarrierHit.TIME:
        t1 = min(t1, deadline)
    fees = charges.round_trip(lot_units, entry, exit_px)
    pnl = (exit_px - entry) * lot_units - fees
    return BarrierLabel(
        t0=t0, t1=max(t1, t0), entry=entry, exit=exit_px, stop=round(stop, 2), target=round(target, 2), hit=hit,
        holding_minutes=round((max(t1, t0) - t0).total_seconds() / 60, 2),
        return_pct=round((exit_px / entry - 1) * 100, 4), r_multiple=round((exit_px - entry) / (entry - stop), 4),
        net_pnl_per_lot=round(pnl, 2), net_return_pct=round(pnl / (entry * lot_units) * 100, 4),
        charges_per_lot=fees, mfe_pct=round((hi_seen / entry - 1) * 100, 4),
        mae_pct=round((lo_seen / entry - 1) * 100, 4))


# --------------------------------------------------------------------------- #
# Forward regime labels (for the regime classifier)
# --------------------------------------------------------------------------- #

TREND_CLASSES = ("TRENDING_DOWN", "RANGE_BOUND", "TRENDING_UP")
VOL_CLASSES = ("LOW_VOL", "NORMAL_VOL", "HIGH_VOL")


@dataclass(frozen=True)
class RegimeLabel:
    """What the NEXT ``horizon`` minutes of NIFTY looked like after ``t0``."""

    t0: datetime
    t1: datetime
    trend: int  # index into TREND_CLASSES
    vol: int  # index into VOL_CLASSES
    forward_return_pct: float
    forward_rv: float  # annualised realised volatility over the horizon


def forward_regime_label(index: Bars, t0: datetime, *, horizon_minutes: int, thresholds: RegimeThresholds,
                         min_coverage: float = 0.8) -> RegimeLabel | None:
    """Label from the bars completed in ``(t0, t0 + horizon]`` of the same
    session (and the last close at or before ``t0`` as the base). ``None`` when
    the session ends first or bars are missing (coverage below ``min_coverage``)."""
    t0 = ensure_ist(t0)
    ts0, ts1 = int(t0.timestamp()), int((t0 + timedelta(minutes=horizon_minutes)).timestamp())
    base_i = int(np.searchsorted(index.end, ts0, side="right")) - 1
    if base_i < 0:
        return None
    day = index.day[base_i]
    lo, hi = base_i + 1, int(np.searchsorted(index.end, ts1, side="right"))
    fwd = index.close[lo:hi][index.day[lo:hi] == day]
    if fwd.size < max(5, int(horizon_minutes * min_coverage)):
        return None
    closes = np.concatenate(([index.close[base_i]], fwd))
    rets = np.diff(np.log(closes))
    rv = float(np.std(rets, ddof=1) * ANNUALISE_1M)
    move = float(np.log(closes[-1] / closes[0]))
    expected = rv / ANNUALISE_1M * np.sqrt(rets.size)
    z = move / expected if expected > 0 else 0.0
    trend = 2 if z > thresholds.trend_sigmas else (0 if z < -thresholds.trend_sigmas else 1)
    vol = 0 if rv < thresholds.low_vol else (2 if rv > thresholds.high_vol else 1)
    t1 = datetime.fromtimestamp(int(index.end[lo:hi][index.day[lo:hi] == day][-1]), IST)
    return RegimeLabel(t0, t1, trend, vol, round(move * 100, 4), round(rv, 5))


__all__ = ["TREND_CLASSES", "VOL_CLASSES", "BarrierHit", "BarrierLabel", "BarrierSpec", "RegimeLabel",
           "forward_regime_label", "triple_barrier"]
