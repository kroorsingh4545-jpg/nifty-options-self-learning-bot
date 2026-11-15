"""Session market regime (R3), rule-based on trailing data (pure).

It EXTENDS the bot's regime vocabulary instead of duplicating it: the output is
an ``ml.regime_classifier.RegimePrediction`` (trend axis TRENDING_DOWN /
RANGE_BOUND / TRENDING_UP, volatility axis LOW_VOL / NORMAL_VOL / HIGH_VOL,
expiry day), with the extra axes, confidence and reasons in ``extra``.
``ml/`` itself is not changed, and no model is trained (no machine learning
until enough real history: owner rule).

Inputs at a moment of a session, all known by then (point in time):

* India VIX now, against the trailing ``lookback`` daily VIX closes before the day
  -> volatility level (terciles of that distribution, not fixed numbers)
* VIX change over ``change_sessions`` sessions, against its own trailing
  distribution -> EXPANSION / STABLE / CONTRACTION
* NIFTY's move over the last ``trend_sessions`` daily closes in units of its
  expected size (1 sd) -> trend (beyond +/- ``trend_z``)
* the weekly expiry calendar -> expiry day
* UNSTABLE when today's opening gap or the VIX change is extreme
  (beyond ``unstable_z`` sd / the ``unstable_pct`` percentile)

Not available with our data, so not classified: event-driven days (no event
calendar is recorded).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ml.labeling import TREND_CLASSES, VOL_CLASSES
from ml.regime_classifier import RegimePrediction

EXPANSION, STABLE, CONTRACTION = "EXPANSION", "STABLE", "CONTRACTION"


@dataclass(frozen=True)
class RegimeSettings:
    lookback: int = 252
    change_sessions: int = 5
    trend_sessions: int = 20
    trend_z: float = 1.0
    unstable_z: float = 3.0
    unstable_pct: float = 0.99


DEFAULT_SETTINGS = RegimeSettings()


def _pct(history: Sequence[float], x: float) -> float:
    h = np.asarray([v for v in history if math.isfinite(v)], dtype=np.float64)
    return float(np.mean(h <= x)) if len(h) else math.nan


def _one_hot(names: tuple[str, ...], idx: int | None) -> dict[str, float]:
    if idx is None:
        return dict.fromkeys(names, round(1 / len(names), 5))
    return {n: 1.0 if i == idx else 0.0 for i, n in enumerate(names)}


def classify(*, vix_now: float, vix_closes_before: Sequence[float], nifty_closes_before: Sequence[float],
             open_today: float | None, expiry_day: bool, s: RegimeSettings = DEFAULT_SETTINGS) -> RegimePrediction:
    """Regime at one moment. ``vix_closes_before`` / ``nifty_closes_before`` are
    daily closes of the sessions BEFORE today, oldest first."""
    reasons: list[str] = []
    vix_hist = list(vix_closes_before)[-s.lookback:]
    vol_idx = None
    lvl = _pct(vix_hist, vix_now) if len(vix_hist) >= s.lookback // 4 and vix_now > 0 else math.nan
    if math.isfinite(lvl):
        vol_idx = 0 if lvl <= 1 / 3 else (2 if lvl >= 2 / 3 else 1)
        reasons.append(f"India VIX {vix_now:.2f} = p{100 * lvl:.0f} of the last {len(vix_hist)} sessions")
    change, chg_pct = STABLE, math.nan
    v = np.asarray(list(vix_closes_before), dtype=np.float64)
    k = s.change_sessions
    if len(v) > k + s.lookback // 4 and vix_now > 0:
        past = np.log(v[k:] / v[:-k])[-s.lookback:]
        now = math.log(vix_now / v[-k])
        chg_pct = _pct(past.tolist(), now)
        change = EXPANSION if chg_pct >= 2 / 3 else (CONTRACTION if chg_pct <= 1 / 3 else STABLE)
        reasons.append(f"VIX {k}-session change {100 * now:+.1f} % = p{100 * chg_pct:.0f}: {change.lower()}")
    trend_idx, z = None, math.nan
    c = np.asarray(list(nifty_closes_before), dtype=np.float64)
    n = s.trend_sessions
    if len(c) > max(n, 60):
        r = np.diff(np.log(c))
        sd = float(np.std(r[-60:], ddof=1))
        z = math.log(c[-1] / c[-1 - n]) / (sd * math.sqrt(n)) if sd > 0 else math.nan
        if math.isfinite(z):
            trend_idx = 2 if z > s.trend_z else (0 if z < -s.trend_z else 1)
            reasons.append(f"NIFTY {n}-session move {z:+.2f} sd")
    unstable = False
    if open_today is not None and len(c) > 61 and open_today > 0:
        r = np.diff(np.log(c))
        sd = float(np.std(r[-60:], ddof=1))
        gap_z = math.log(open_today / c[-1]) / sd if sd > 0 else 0.0
        if abs(gap_z) > s.unstable_z:
            unstable = True
            reasons.append(f"opening gap {gap_z:+.1f} sd")
    if math.isfinite(chg_pct) and chg_pct >= s.unstable_pct:
        unstable = True
        reasons.append("VIX jump in the top 1 % of its history")
    known = [x for x in (lvl, chg_pct) if math.isfinite(x)] + ([min(1.0, abs(z) / 3)] if math.isfinite(z) else [])
    # confidence: how far the deciding measures sit from their tercile / trend boundaries (0 = on a boundary)
    dists = []
    if math.isfinite(lvl):
        dists.append(min(abs(lvl - 1 / 3), abs(lvl - 2 / 3)) * 3)
    if math.isfinite(z):
        dists.append(min(1.0, abs(abs(z) - s.trend_z)))
    conf = round(float(np.clip(np.mean(dists), 0, 1)), 3) if dists else 0.0
    extra = {"vol_change": change, "unstable": unstable, "confidence": conf, "reasons": reasons,
             "vix_percentile": None if not math.isfinite(lvl) else round(lvl, 3),
             "vix_change_percentile": None if not math.isfinite(chg_pct) else round(chg_pct, 3),
             "trend_z": None if not math.isfinite(z) else round(z, 3), "inputs_known": len(known)}
    return RegimePrediction(_one_hot(TREND_CLASSES, trend_idx), _one_hot(VOL_CLASSES, vol_idx), expiry_day,
                            source="rules-d4", extra=extra)


def named(p: RegimePrediction) -> str:
    """One of the R3 regime names: UNSTABLE, VOL_EXPANSION, VOL_CONTRACTION, or
    {LOW|NORMAL|HIGH}_VOL_{TRENDING|RANGE}; UNKNOWN when an axis is missing."""
    if p.extra.get("unstable"):
        return "UNSTABLE"
    if p.extra.get("vol_change") == EXPANSION:
        return "VOL_EXPANSION"
    if p.extra.get("vol_change") == CONTRACTION:
        return "VOL_CONTRACTION"
    if len(set(p.vol.values())) == 1 or len(set(p.trend.values())) == 1:
        return "UNKNOWN"
    trend = "RANGE" if p.trend_label == "RANGE_BOUND" else "TRENDING"
    return f"{p.vol_label.removesuffix('_VOL')}_VOL_{trend}"


__all__ = ["CONTRACTION", "DEFAULT_SETTINGS", "EXPANSION", "STABLE", "RegimeSettings", "classify", "named"]
