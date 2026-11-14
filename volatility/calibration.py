"""Forecast-quality statistics: how well did predicted volatility describe what
actually happened?

* ``band_hit_rate``: share of outcomes inside +/- k forecast sigma, with a
  Wilson 95 % interval, against the normal-distribution reference.
* ``variance_ratio``: mean realised variance / mean forecast variance
  (1 = unbiased; < 1 = the forecast was too high, i.e. "implied > realised").
* ``qlike``: Patton's (2011) robust loss ``proxy / h + ln h`` (lower is
  better); it ranks forecasts correctly even with a noisy proxy such as a
  squared daily return.
* ``mincer_zarnowitz``: OLS ``proxy = a + b * forecast`` (b = 1, a = 0 ideal).
* ``block_bootstrap_ratio_ci``: moving-block bootstrap interval for a ratio of
  means, because daily volatility errors are autocorrelated (clustered).
* ``exceedance_clustering``: P(|z| > k tomorrow | |z| > k today) vs the
  unconditional rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Float = NDArray[np.float64]


def _finite(*xs: object) -> list[Float]:
    arrs = [np.asarray(x, dtype=np.float64) for x in xs]
    mask = np.ones(arrs[0].shape, dtype=bool)
    for a in arrs:
        mask &= np.isfinite(a)
    return [a[mask] for a in arrs]


def normal_coverage(k: float) -> float:
    """P(|Z| <= k) for a standard normal Z."""
    return math.erf(k / math.sqrt(2.0))


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (math.nan, math.nan)
    p = hits / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class HitRate:
    k: float
    n: int
    hits: int
    rate: float
    low: float
    high: float
    normal: float

    def as_dict(self) -> dict[str, object]:
        return {"k": self.k, "n": self.n, "rate": round(self.rate, 4), "ci95": [round(self.low, 4),
                round(self.high, 4)], "normal": round(self.normal, 4)}


def band_hit_rate(z: object, k: float) -> HitRate:
    (zz,) = _finite(z)
    n = int(len(zz))
    hits = int(np.sum(np.abs(zz) <= k))
    lo, hi = wilson_interval(hits, n)
    return HitRate(k, n, hits, hits / n if n else math.nan, lo, hi, normal_coverage(k))


def variance_ratio(realised_var: object, forecast_var: object) -> float:
    r, f = _finite(realised_var, forecast_var)
    return float(np.mean(r) / np.mean(f)) if len(r) and np.mean(f) > 0 else math.nan


def block_bootstrap_ratio_ci(num: object, den: object, *, block: int = 20, n_boot: int = 1000, seed: int = 7,
                             level: float = 0.95) -> tuple[float, float]:
    """Moving-block bootstrap interval of ``mean(num) / mean(den)``."""
    a, b = _finite(num, den)
    n = len(a)
    if n < 2 * block:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)[:, :n]
    ratios = a[idx].mean(axis=1) / b[idx].mean(axis=1)
    lo, hi = np.quantile(ratios, [(1 - level) / 2, 1 - (1 - level) / 2])
    return (float(lo), float(hi))


def qlike(proxy_var: object, forecast_var: object) -> float:
    p, h = _finite(proxy_var, forecast_var)
    ok = h > 0
    return float(np.mean(p[ok] / h[ok] + np.log(h[ok]))) if np.any(ok) else math.nan


def mse(proxy_var: object, forecast_var: object) -> float:
    p, h = _finite(proxy_var, forecast_var)
    return float(np.mean((p - h) ** 2)) if len(p) else math.nan


@dataclass(frozen=True)
class MZ:
    a: float
    b: float
    r2: float
    n: int


def mincer_zarnowitz(proxy_var: object, forecast_var: object) -> MZ:
    p, h = _finite(proxy_var, forecast_var)
    n = len(p)
    if n < 3 or float(np.var(h)) == 0.0:
        return MZ(math.nan, math.nan, math.nan, n)
    x = np.column_stack([np.ones(n), h])
    coef, *_ = np.linalg.lstsq(x, p, rcond=None)
    resid = p - x @ coef
    r2 = 1.0 - float(np.sum(resid**2)) / float(np.sum((p - p.mean()) ** 2))
    return MZ(float(coef[0]), float(coef[1]), r2, n)


def exceedance_clustering(z: object, k: float) -> tuple[float, float, int]:
    """(P(|z_{t+1}| > k  |  |z_t| > k), P(|z| > k), number of exceedances)."""
    zz = np.asarray(z, dtype=np.float64)
    exc = np.abs(zz) > k
    valid = np.isfinite(zz[:-1]) & np.isfinite(zz[1:])
    today, tomorrow = exc[:-1] & valid, exc[1:] & valid
    n_exc = int(np.sum(today))
    base = float(np.mean(exc[np.isfinite(zz)])) if np.any(np.isfinite(zz)) else math.nan
    cond = float(np.sum(today & tomorrow) / n_exc) if n_exc else math.nan
    return cond, base, n_exc


def excess_kurtosis(z: object) -> float:
    (x,) = _finite(z)
    if len(x) < 4:
        return math.nan
    m = x.mean()
    s2 = np.mean((x - m) ** 2)
    return float(np.mean((x - m) ** 4) / s2**2 - 3.0)


__all__ = ["MZ", "HitRate", "band_hit_rate", "block_bootstrap_ratio_ci", "exceedance_clustering",
           "excess_kurtosis", "mincer_zarnowitz", "mse", "normal_coverage", "qlike", "variance_ratio",
           "wilson_interval"]
