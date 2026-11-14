"""Realised-volatility estimators (pure numpy).

Daily OHLC estimators (per-session variance; multiply by ``TRADING_DAYS`` and
take the square root to annualise):

* close-to-close: ``r_t**2`` with ``r_t = ln(C_t / C_{t-1})``
* Parkinson (1980): ``ln(H/L)**2 / (4 ln 2)``, intraday only (no overnight gap)
* Garman-Klass (1980): ``0.5 ln(H/L)**2 - (2 ln 2 - 1) ln(C/O)**2``, intraday only
* Yang-Zhang (2000): overnight + open-to-close + Rogers-Satchell, over a window,
  with the weight ``k = 0.34 / (1.34 + (n + 1) / (n - 1))``

Range estimators assume the true high and low of a continuous path. Highs
and lows observed on a discrete path understate the range, so Parkinson and
Garman-Klass read LOW on coarse data (about 10 % for Garman-Klass with 375
observations a session; tested). Treat them as lower bounds when comparing
with close-to-close variance.

Intraday realised variance: the sum of squared log returns of minute closes
sampled every ``step`` minutes inside one session. Sampling at 5 minutes is the
default because the 1-minute index level is computed from constituent prices
that do not all trade every minute (stale prices bias very fine sampling).
"""

from __future__ import annotations

import math
from typing import cast

import numpy as np
from numpy.typing import NDArray

from volatility import TRADING_DAYS

Float = NDArray[np.float64]
LN2 = math.log(2.0)


def _a(x: object) -> Float:
    return cast(Float, np.asarray(x, dtype=np.float64))


def log_returns(close: object) -> Float:
    """``ln(C_t / C_{t-1})``; the first element is NaN."""
    c = _a(close)
    out = np.full(c.shape, np.nan)
    out[1:] = np.log(c[1:] / c[:-1])
    return out


def parkinson_var(high: object, low: object) -> Float:
    return np.log(_a(high) / _a(low)) ** 2 / (4.0 * LN2)


def garman_klass_var(open_: object, high: object, low: object, close: object) -> Float:
    hl = np.log(_a(high) / _a(low))
    co = np.log(_a(close) / _a(open_))
    return np.maximum(0.5 * hl**2 - (2.0 * LN2 - 1.0) * co**2, 0.0)


def rogers_satchell_var(open_: object, high: object, low: object, close: object) -> Float:
    o, h, lo, c = _a(open_), _a(high), _a(low), _a(close)
    return cast(Float, np.log(h / c) * np.log(h / o) + np.log(lo / c) * np.log(lo / o))


def rolling_mean(x: object, n: int) -> Float:
    """Trailing mean over ``n`` values ending at each index (NaN until ``n``
    finite values exist; a NaN inside the window makes the result NaN)."""
    if n < 1:
        raise ValueError("window must be >= 1")
    v = _a(x)
    out = np.full(v.shape, np.nan)
    if len(v) < n:
        return out
    csum = np.cumsum(np.where(np.isnan(v), 0.0, v))
    cnan = np.cumsum(np.isnan(v))
    s = csum[n - 1:] - np.concatenate(([0.0], csum[:-n]))
    bad = cnan[n - 1:] - np.concatenate(([0], cnan[:-n])) > 0
    out[n - 1:] = np.where(bad, np.nan, s / n)
    return out


def close_to_close_vol(close: object, n: int, *, periods: int = TRADING_DAYS) -> Float:
    """Trailing annualised close-to-close volatility over ``n`` returns."""
    r = log_returns(close)
    return np.sqrt(rolling_mean(r**2, n) * periods)


def yang_zhang_var(open_: object, high: object, low: object, close: object, n: int) -> Float:
    """Trailing per-session Yang-Zhang variance over ``n`` sessions (needs the
    previous close, so the first value is NaN)."""
    if n < 2:
        raise ValueError("Yang-Zhang needs n >= 2")
    o, c = _a(open_), _a(close)
    prev_c = np.concatenate(([np.nan], c[:-1]))
    on = np.log(o / prev_c)
    oc = np.log(c / o)
    rs = rogers_satchell_var(o, high, low, c)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    def sample_var(x: Float) -> Float:
        m = rolling_mean(x, n)
        return rolling_mean(x**2, n) * n / (n - 1) - m**2 * n / (n - 1)

    return sample_var(on) + k * sample_var(oc) + (1.0 - k) * rolling_mean(rs, n)


def annualise(per_period_var: object, periods: int = TRADING_DAYS) -> Float:
    return np.sqrt(_a(per_period_var) * periods)


def intraday_realised_var(session_open: float, minute_closes: object, *, step: int = 5) -> float:
    """Sum of squared log returns of one session's minute closes sampled every
    ``step`` minutes, starting from the session open price. NaN when fewer
    than two prices are available."""
    c = _a(minute_closes)
    if step < 1:
        raise ValueError("step must be >= 1")
    if len(c) < 1 or not math.isfinite(session_open) or session_open <= 0:
        return math.nan
    sampled = np.concatenate(([session_open], c[step - 1::step]))
    if len(c) % step:
        sampled = np.concatenate((sampled, [c[-1]]))
    r = np.diff(np.log(sampled))
    return float(np.sum(r**2)) if len(r) else math.nan


def ewma_var(returns: object, lam: float = 0.94, *, seed_n: int = 20) -> Float:
    """RiskMetrics EWMA: the forecast for index t uses returns up to t-1 only.
    Seeded with the mean square of the first ``seed_n`` returns."""
    if not 0.0 < lam < 1.0:
        raise ValueError("lambda must be in (0, 1)")
    r = _a(returns)
    out = np.full(r.shape, np.nan)
    finite = np.flatnonzero(np.isfinite(r))
    if len(finite) <= seed_n:
        return out
    start = finite[seed_n - 1]
    s2 = float(np.nanmean(r[finite[:seed_n]] ** 2))
    for t in range(start + 1, len(r)):
        out[t] = s2
        x = r[t]
        if math.isfinite(x):
            s2 = lam * s2 + (1.0 - lam) * x * x
    return out


__all__ = ["annualise", "close_to_close_vol", "ewma_var", "garman_klass_var", "intraday_realised_var",
           "log_returns", "parkinson_var", "rogers_satchell_var", "rolling_mean", "yang_zhang_var"]
