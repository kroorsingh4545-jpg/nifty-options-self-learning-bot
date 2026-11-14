"""Implied-volatility surface helpers: ATM IV, skew, term structure, IV rank
and percentile. Inputs are plain values; IV is a DECIMAL here (0.13 = 13 %).
Convert percent sources (the Upstox REST chain) before calling.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np


@dataclass(frozen=True)
class IvPoint:
    strike: float
    option_type: str  # CE | PE
    iv: float | None  # decimal


def atm_iv(points: Sequence[IvPoint], spot: float) -> float | None:
    """Mean of the CE and PE IV at the listed strike nearest ``spot`` (one
    side if only one has a usable IV); ``None`` when neither does."""
    usable = [p for p in points if p.iv is not None and math.isfinite(p.iv) and p.iv > 0]
    if not usable or spot <= 0:
        return None
    strike = min({p.strike for p in usable}, key=lambda k: (abs(k - spot), k))
    ivs = [p.iv for p in usable if p.strike == strike and p.iv is not None]
    return float(np.mean(ivs))


def put_call_skew(points: Sequence[IvPoint], spot: float, offset: float) -> float | None:
    """IV(put at the strike nearest spot - offset) - IV(call nearest spot + offset)."""
    def nearest(opt: str, target: float) -> float | None:
        side = [p for p in points if p.option_type == opt and p.iv is not None and p.iv > 0]
        if not side:
            return None
        return min(side, key=lambda p: (abs(p.strike - target), p.strike)).iv

    put, call = nearest("PE", spot - offset), nearest("CE", spot + offset)
    return None if put is None or call is None else put - call


def term_structure(atm_by_expiry: Mapping[date, float | None]) -> list[tuple[date, float]]:
    return sorted((e, v) for e, v in atm_by_expiry.items() if v is not None and math.isfinite(v))


def percentile_rank(history: Iterable[float], value: float) -> float:
    """Share of ``history`` strictly below ``value`` (0..1); NaN if empty."""
    h = np.asarray([x for x in history if math.isfinite(x)], dtype=np.float64)
    return float(np.mean(h < value)) if len(h) else math.nan


def iv_rank(history: Iterable[float], value: float) -> float:
    """(value - min) / (max - min) over ``history`` (0..1); NaN if flat or empty."""
    h = np.asarray([x for x in history if math.isfinite(x)], dtype=np.float64)
    if not len(h):
        return math.nan
    lo, hi = float(np.min(h)), float(np.max(h))
    if hi == lo:
        return math.nan
    return float(np.clip((value - lo) / (hi - lo), 0.0, 1.0))


__all__ = ["IvPoint", "atm_iv", "iv_rank", "percentile_rank", "put_call_skew", "term_structure"]
