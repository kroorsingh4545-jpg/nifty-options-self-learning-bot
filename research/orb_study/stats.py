"""Metrics and the pre-registered pass rule of ORB study E1 (pure).

Pass rule (``STUDY_E1["pass_rule"]``), on per-trade results (rupees per lot
after costs, or a paired per-trade difference): (1) mean > 0; (2) mean > 0 in
at least 3 of the 4 calendar years 2022-2025; (3) the day-block bootstrap 95 %
percentile interval of the mean excludes zero. A pass whose Bonferroni
interval (alpha = 0.05 / number of tests run) includes zero is WEAK.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np

SEED = 20261121
DRAWS = 10_000
TEST_YEARS = (2022, 2023, 2024, 2025)
MIN_YEARS = 3


def _r(x: float, k: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), k)


def basic(values: Sequence[float]) -> dict[str, Any]:
    v = np.asarray(values, dtype=float)
    n = int(v.size)
    if not n:
        return {"trades": 0}
    wins, losses = v[v > 0], v[v < 0]
    n_win, n_loss = int(wins.size), int(losses.size)
    pf = float(np.sum(wins)) / -float(np.sum(losses)) if n_loss else math.inf
    return {"trades": n, "total": _r(float(np.sum(v))), "mean": _r(float(np.mean(v)), 3),
            "win_rate": _r(100.0 * n_win / n, 1), "profit_factor": _r(pf, 3) if math.isfinite(pf) else None,
            "avg_win": _r(float(np.mean(wins))) if n_win else None,
            "avg_loss": _r(float(np.mean(losses))) if n_loss else None}


def daily(days: Sequence[date], values: Sequence[float]) -> tuple[list[date], np.ndarray, np.ndarray]:
    """(sorted days, sum per day, trades per day)."""
    acc: dict[date, list[float]] = {}
    for d, x in zip(days, values, strict=True):
        acc.setdefault(d, []).append(float(x))
    ds = sorted(acc)
    return ds, np.asarray([sum(acc[d]) for d in ds]), np.asarray([len(acc[d]) for d in ds], dtype=float)


def max_drawdown(day_sums: np.ndarray) -> float:
    if not day_sums.size:
        return 0.0
    cum = np.cumsum(day_sums)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float(np.max(peak - cum))


def top_share(day_sums: np.ndarray, share: float = 0.10) -> float | None:
    """Share of the total result that came from the best ``share`` of days."""
    total = float(day_sums.sum())
    if not day_sums.size or total <= 0:
        return None
    k = max(1, math.ceil(share * day_sums.size))
    return float(np.sort(day_sums)[::-1][:k].sum() / total)


def summary(days: Sequence[date], values: Sequence[float]) -> dict[str, Any]:
    """``basic`` overall and by year, max drawdown, top-10 % day share."""
    out = basic(values)
    ds, sums, _ = daily(days, values)
    out["max_drawdown"] = _r(max_drawdown(sums))
    ts = top_share(sums)
    out["top10pct_days_share"] = _r(ts, 3) if ts is not None else None
    years = sorted({d.year for d in days})
    out["by_year"] = {str(y): basic([x for d, x in zip(days, values, strict=True) if d.year == y]) for y in years}
    return out


def bootstrap_means(days: Sequence[date], values: Sequence[float], *, draws: int = DRAWS,
                    seed: int = SEED) -> np.ndarray:
    """Day-block bootstrap of the mean per trade."""
    _, sums, counts = daily(days, values)
    if not sums.size:
        return np.asarray([])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, sums.size, size=(draws, sums.size))
    return np.asarray(sums[idx].sum(axis=1) / counts[idx].sum(axis=1))


def pass_rule(days: Sequence[date], values: Sequence[float], *, draws: int = DRAWS, seed: int = SEED) -> dict[str, Any]:
    """The three pre-registered conditions; ``bonferroni`` is filled in later (``apply_bonferroni``)."""
    v = np.asarray(values, dtype=float)
    if not v.size:
        return {"n": 0, "passed": False, "reason": "no trades"}
    year_means = {}
    for y in TEST_YEARS:
        sel = [x for d, x in zip(days, values, strict=True) if d.year == y]
        year_means[str(y)] = _r(float(np.mean(sel)), 3) if sel else None
    good_years = sum(1 for m in year_means.values() if m is not None and m > 0)
    boot = bootstrap_means(days, values, draws=draws, seed=seed)
    lo, hi = (float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5)))
    mean = float(v.mean())
    c1, c2, c3 = mean > 0, good_years >= MIN_YEARS, lo > 0
    return {"n": int(v.size), "mean": _r(mean, 3), "ci95": [_r(lo, 3), _r(hi, 3)], "year_means": year_means,
            "positive_years": good_years, "after_costs_positive": c1, "years_ok": c2, "ci_excludes_zero": c3,
            "passed": bool(c1 and c2 and c3), "_boot": boot}


def apply_bonferroni(tests: dict[str, dict[str, Any]], alpha: float = 0.05) -> int:
    """Adds the Bonferroni interval and the WEAK / PASS / FAIL verdict to every test; returns the count."""
    m = sum(1 for t in tests.values() if t.get("n"))
    a = alpha / max(m, 1)
    for t in tests.values():
        boot = t.pop("_boot", None)
        if not t.get("n") or boot is None or not len(boot):
            t["verdict"] = "FAIL"
            continue
        lo, hi = float(np.percentile(boot, 100 * a / 2)), float(np.percentile(boot, 100 * (1 - a / 2)))
        t["ci_bonferroni"] = [_r(lo, 3), _r(hi, 3)]
        t["verdict"] = ("PASS" if lo > 0 else "WEAK") if t["passed"] else "FAIL"
    return m


__all__ = ["DRAWS", "SEED", "TEST_YEARS", "apply_bonferroni", "basic", "bootstrap_means", "daily", "max_drawdown",
           "pass_rule", "summary", "top_share"]
