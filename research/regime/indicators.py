"""Trailing intraday indicators on one session's 1-minute bars (pure numpy).

Every series value at index ``i`` uses bars ``0..i`` of the SAME session only
(no overnight mixing, no look-ahead); NaN until enough bars exist.

* ``efficiency_ratio`` (Kaufman): ``|C_i - C_{i-n}| / sum |C_k - C_{k-1}|`` over
  the last ``n`` bars: near 1 = an efficient one-way move, near 0 = chop.
* ``true_range``, ``atr`` (Wilder smoothing), ``atr_expansion`` = short ATR /
  long ATR (> 1: ranges widening).
* ``adx`` (Wilder, period ``n``): trend strength regardless of direction.
* ``displacement``: ``(C_i - C_{i-n}) / ATR_i`` (signed, in ATR units).
* ``realised_sd``: sample sd of the last ``n`` 1-minute log returns.
"""

from __future__ import annotations

import math
from typing import cast

import numpy as np
from numpy.typing import NDArray

Float = NDArray[np.float64]


def _a(x: object) -> Float:
    return cast(Float, np.asarray(x, dtype=np.float64))


def efficiency_ratio(close: object, n: int) -> Float:
    c = _a(close)
    out = np.full(c.shape, np.nan)
    if n < 1 or len(c) <= n:
        return out
    step = np.abs(np.diff(c))
    path = np.convolve(step, np.ones(n), mode="valid")  # sum of the last n steps, ending at index n..len-1
    net = np.abs(c[n:] - c[:-n])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n:] = np.where(path > 0, net / path, 0.0)
    return out


def true_range(high: object, low: object, close: object) -> Float:
    h, lo, c = _a(high), _a(low), _a(close)
    prev = np.concatenate(([np.nan], c[:-1]))
    tr = np.fmax(h - lo, np.fmax(np.abs(h - prev), np.abs(lo - prev)))
    tr[0] = h[0] - lo[0]  # the session's first bar has no previous close in the session
    return tr


def wilder(x: object, n: int) -> Float:
    """Wilder smoothing: SMA of the first ``n`` values, then ``prev + (x - prev) / n``."""
    v = _a(x)
    out = np.full(v.shape, np.nan)
    if n < 1 or len(v) < n:
        return out
    s = float(np.mean(v[:n]))
    out[n - 1] = s
    for i in range(n, len(v)):
        s += (v[i] - s) / n
        out[i] = s
    return out


def atr(high: object, low: object, close: object, n: int) -> Float:
    return wilder(true_range(high, low, close), n)


def atr_expansion(high: object, low: object, close: object, short: int, long: int) -> Float:
    if not 1 <= short < long:
        raise ValueError("need 1 <= short < long")
    s, lg = atr(high, low, close, short), atr(high, low, close, long)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(lg > 0, s / lg, np.nan)


def adx(high: object, low: object, close: object, n: int) -> Float:
    """Wilder's ADX; the first value appears at index ``2n - 1``."""
    h, lo = _a(high), _a(low)
    out = np.full(h.shape, np.nan)
    if len(h) < 2 * n:
        return out
    up, down = np.diff(h), -np.diff(lo)
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(h, lo, close)[1:]
    atr_ = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(plus, n) / atr_
        mdi = 100 * wilder(minus, n) / atr_
        dx = np.where(pdi + mdi > 0, 100 * np.abs(pdi - mdi) / (pdi + mdi), 0.0)
    first = n - 1  # first valid dx index (in the diff-shifted series)
    adx_ = np.full(dx.shape, np.nan)
    if len(dx) >= first + n:
        adx_[first + n - 1:] = wilder(dx[first:], n)[n - 1:]
    out[1:] = adx_
    return out


def displacement(high: object, low: object, close: object, n: int, atr_n: int) -> Float:
    c = _a(close)
    out = np.full(c.shape, np.nan)
    a = atr(high, low, c, atr_n)
    if len(c) > n:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n:] = np.where(a[n:] > 0, (c[n:] - c[:-n]) / a[n:], np.nan)
    return out


def realised_sd(close: object, n: int) -> Float:
    c = _a(close)
    out = np.full(c.shape, np.nan)
    if len(c) <= n or n < 2:
        return out
    r = np.diff(np.log(c))
    for i in range(n, len(c)):
        out[i] = float(np.std(r[i - n:i], ddof=1))
    return out


def safe(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


__all__ = ["adx", "atr", "atr_expansion", "displacement", "efficiency_ratio", "realised_sd", "safe", "true_range",
           "wilder"]
