"""Momentum Pulse: is the market moving for real, or chopping? (pure)

Four trailing components on the session's 1-minute bars
(``research.regime.indicators``), never one alone:

* ``er``: Kaufman efficiency ratio over ``lookback`` bars
* ``adx``: Wilder ADX (trend strength, either direction)
* ``abs_disp``: |displacement| over ``lookback`` bars in ATR units
* ``atrx``: ATR expansion (short ATR / long ATR)

Thresholds are never fixed numbers: each component is turned into its
percentile against a TRAINING distribution (``fit``), so a cutoff means "in the
top 30 % of what this market did in the training years". The percentile levels
(``HI``, ``LO``, ``MID``, ``EXPANDING``) are fixed in advance (pre-registered) and
are not tuned on test data.

Labels: STRONG_TREND, DEVELOPING, WEAK, RANGE_CHOP, UNCERTAIN (a component is
missing: too few bars). Every label carries a direction (sign of the
displacement), a confidence in [0, 1] (how far the deciding components sit
beyond their cutoffs) and the reasons.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np

COMPONENTS = ("er", "adx", "abs_disp", "atrx")
HI, LO, MID, EXPANDING = 0.7, 0.3, 0.5, 0.6
STRONG_TREND, DEVELOPING, WEAK, RANGE_CHOP, UNCERTAIN = (
    "STRONG_TREND", "DEVELOPING", "WEAK", "RANGE_CHOP", "UNCERTAIN")
LABELS = (STRONG_TREND, DEVELOPING, WEAK, RANGE_CHOP, UNCERTAIN)
NAMES = {"er": "efficiency ratio", "adx": "ADX", "abs_disp": "|displacement| (ATR)", "atrx": "ATR expansion"}


@dataclass(frozen=True)
class Cutoffs:
    """Sorted training values of each component (the percentile reference)."""

    values: Mapping[str, np.ndarray]
    n_train: int

    def percentile(self, comp: str, x: float) -> float:
        v = self.values[comp]
        return float(np.searchsorted(v, x, side="right")) / len(v) if len(v) else math.nan


@dataclass(frozen=True)
class Pulse:
    label: str
    direction: int  # +1 up, -1 down, 0 none
    confidence: float
    reasons: tuple[str, ...]
    percentiles: Mapping[str, float] = field(default_factory=dict)


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(float(x))


def fit(features: Iterable[Mapping[str, float]], *, min_train: int = 200) -> Cutoffs:
    rows = list(features)
    vals = {c: np.sort(np.asarray([float(r[c]) for r in rows if _finite(r.get(c))], dtype=np.float64))
            for c in COMPONENTS}
    n = min(len(v) for v in vals.values()) if vals else 0
    if n < min_train:
        raise ValueError(f"too few training samples to fit Momentum Pulse cutoffs ({n} < {min_train})")
    return Cutoffs(vals, n)


def pulse(f: Mapping[str, float], cut: Cutoffs) -> Pulse:
    if not all(_finite(f.get(c)) for c in COMPONENTS):
        missing = [NAMES[c] for c in COMPONENTS if not _finite(f.get(c))]
        return Pulse(UNCERTAIN, 0, 0.0, (f"not enough bars for {', '.join(missing)}",))
    p = {c: cut.percentile(c, float(f[c])) for c in COMPONENTS}
    disp = float(f.get("disp", 0.0) or 0.0)
    direction = 1 if disp > 0 else (-1 if disp < 0 else 0)
    reasons = tuple(f"{NAMES[c]} {float(f[c]):.3g} = p{100 * p[c]:.0f} of training" for c in COMPONENTS)

    def beyond_hi(*cs: str) -> float:
        return min((p[c] - HI) / (1 - HI) for c in cs)

    if p["er"] >= HI and p["adx"] >= HI and p["abs_disp"] >= HI:
        return Pulse(STRONG_TREND, direction, round(beyond_hi("er", "adx", "abs_disp"), 3), reasons, p)
    if p["er"] <= LO and p["adx"] <= LO and p["abs_disp"] <= MID:
        conf = min((LO - p["er"]) / LO, (LO - p["adx"]) / LO)
        return Pulse(RANGE_CHOP, direction, round(conf, 3), reasons, p)
    if p["er"] >= MID and p["abs_disp"] >= MID and p["atrx"] >= EXPANDING:
        conf = min((p["er"] - MID) / (1 - MID), (p["abs_disp"] - MID) / (1 - MID),
                   (p["atrx"] - EXPANDING) / (1 - EXPANDING))
        return Pulse(DEVELOPING, direction, round(conf, 3), reasons, p)
    centre = float(np.mean([p[c] for c in COMPONENTS]))
    return Pulse(WEAK, direction, round(max(0.0, 1 - 2 * abs(centre - 0.5)), 3), reasons, p)


__all__ = ["COMPONENTS", "DEVELOPING", "EXPANDING", "HI", "LABELS", "LO", "MID", "RANGE_CHOP", "STRONG_TREND",
           "UNCERTAIN", "WEAK", "Cutoffs", "Pulse", "fit", "pulse"]
