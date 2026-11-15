"""Purged walk-forward validation of regime and Momentum Pulse hypotheses,
out of sample by calendar year (pure).

Folds: for each test year Y after the first, train on every sample BEFORE Y
(expanding window, walk-forward), with the same purge as
``backtest.purged_cv.PurgedKFold(forward_only=True)``: a training sample whose
[t0, t1] overlaps the test period is dropped, and an ``embargo`` after the test
period applies to nothing (training is strictly earlier). Momentum Pulse
cutoffs are fitted on the training samples only and applied unchanged to the
test year.

Hypotheses are fixed in advance (``HYPOTHESES``): each compares an outcome
between two groups of samples. A hypothesis is KEPT only when it is STABLE:

* the gain (mean of group A - mean of group B) has the expected sign in at
  least ``required_years`` of the test years, each with at least ``min_group``
  samples per group (a thinner year counts as a failure), and
* the pooled out-of-sample gain's 95 % interval (bootstrap over whole days)
  excludes zero with that sign.

Everything else is DROPPED, with the reason. Two-sided hypotheses take the sign
of the pooled gain as their direction. With several hypotheses some will pass
by chance: a kept hypothesis is a candidate for shadow evaluation (D7), not a
trading rule.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from research.regime.pulse import DEVELOPING, RANGE_CHOP, STRONG_TREND, Cutoffs, Pulse, fit, pulse
from research.regime.samples import Sample

Group = Callable[[Sample, Pulse], bool]


@dataclass(frozen=True)
class Hypothesis:
    name: str
    description: str
    kind: str  # CHECKPOINT | ORB
    outcome: str
    group_a: Group
    group_b: Group
    two_sided: bool = False  # False: the gain must be positive


def _trend_aligned(s: Sample) -> bool:
    t = s.regime.get("trend")
    return (t == "TRENDING_UP" and s.direction > 0) or (t == "TRENDING_DOWN" and s.direction < 0)


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis("pulse_continuation", "strong trends continue more than range-chop (checkpoints, 30 min)",
               "CHECKPOINT", "cont", lambda s, p: p.label == STRONG_TREND, lambda s, p: p.label == RANGE_CHOP),
    Hypothesis("pulse_orb_confirmation",
               "ORB signals with a strong or developing pulse in their direction continue more than the rest",
               "ORB", "cont", lambda s, p: p.label in (STRONG_TREND, DEVELOPING) and p.direction == s.direction,
               lambda s, p: not (p.label in (STRONG_TREND, DEVELOPING) and p.direction == s.direction)),
    Hypothesis("existing_trailing_trend",
               "the bot's 30-bar trailing trend rule (ml/feature_engineering.trailing_regime): a trend label in the "
               "move's direction continues more than a range label", "CHECKPOINT", "cont",
               lambda s, p: s.features["ml_trend"] != 0 and s.features["ml_trend"] == s.direction,
               lambda s, p: s.features["ml_trend"] == 0),
    Hypothesis("pulse_volatility", "trending pulse states are followed by more variance (vs trailing) than chop",
               "CHECKPOINT", "log_rv_trailing", lambda s, p: p.label in (STRONG_TREND, DEVELOPING),
               lambda s, p: p.label == RANGE_CHOP),
    Hypothesis("vol_change_iv_bias", "realised / VIX-implied variance differs between VIX expansion and contraction",
               "CHECKPOINT", "log_rv_implied", lambda s, p: s.regime.get("vol_change") == "EXPANSION",
               lambda s, p: s.regime.get("vol_change") == "CONTRACTION", two_sided=True),
    Hypothesis("vol_level_iv_bias", "realised / VIX-implied variance differs between high and low VIX regimes",
               "CHECKPOINT", "log_rv_implied", lambda s, p: s.regime.get("vol") == "HIGH_VOL",
               lambda s, p: s.regime.get("vol") == "LOW_VOL", two_sided=True),
    Hypothesis("orb_daily_trend", "ORB signals in the direction of the daily trend continue more than the rest",
               "ORB", "cont", lambda s, p: _trend_aligned(s), lambda s, p: not _trend_aligned(s)),
)


def purge_forward(train_idx: Sequence[int], samples: Sequence[Sample], test_lo: datetime,
                  test_hi: datetime) -> tuple[list[int], list[int]]:
    """(kept, purged): training samples whose [t0, t1] overlaps [test_lo, test_hi] are purged."""
    kept: list[int] = []
    purged: list[int] = []
    for i in train_idx:
        s = samples[i]
        (purged if s.t0 <= test_hi and s.t1 >= test_lo else kept).append(i)
    return kept, purged


@dataclass(frozen=True)
class Fold:
    year: int
    train: list[int]
    test: list[int]
    purged: list[int]
    cutoffs: Cutoffs


def year_folds(samples: Sequence[Sample], *, min_train_years: int = 1, min_train: int = 200) -> list[Fold]:
    """Expanding walk-forward folds by calendar year; samples must be in time order."""
    years = sorted({s.day.year for s in samples})
    folds = []
    for y in years[min_train_years:]:
        test = [i for i, s in enumerate(samples) if s.day.year == y]
        before = [i for i, s in enumerate(samples) if s.day.year < y]
        if not test or not before:
            continue
        lo, hi = min(samples[i].t0 for i in test), max(samples[i].t1 for i in test)
        train, purged = purge_forward(before, samples, lo, hi)
        cps = [samples[i].features for i in train if samples[i].kind == "CHECKPOINT"]
        folds.append(Fold(y, train, test, purged, fit(cps, min_train=min_train)))
    return folds


def _gain(rows: Sequence[tuple[bool, bool, float]]) -> tuple[float, int, int]:
    a = [v for in_a, _, v in rows if in_a and math.isfinite(v)]
    b = [v for _, in_b, v in rows if in_b and math.isfinite(v)]
    g = float(np.mean(a) - np.mean(b)) if a and b else math.nan
    return g, len(a), len(b)


def _bootstrap(by_day: Mapping[Any, list[tuple[bool, bool, float]]], *, n_boot: int, seed: int) -> tuple[float, float]:
    """Bootstrap over whole days (rows of a day stay together): per-day sums and
    counts of each group, resampled with replacement, give the gain of each draw."""
    days = list(by_day)
    if len(days) < 20:
        return (math.nan, math.nan)
    sa, na, sb, nb = (np.zeros(len(days)) for _ in range(4))
    for k, d in enumerate(days):
        for in_a, in_b, v in by_day[d]:
            if math.isfinite(v):
                if in_a:
                    sa[k] += v
                    na[k] += 1
                if in_b:
                    sb[k] += v
                    nb[k] += 1
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(days), size=(n_boot, len(days)))
    den_a, den_b = na[pick].sum(axis=1), nb[pick].sum(axis=1)
    ok = (den_a > 0) & (den_b > 0)
    if ok.sum() < n_boot // 2:
        return (math.nan, math.nan)
    gains = sa[pick].sum(axis=1)[ok] / den_a[ok] - sb[pick].sum(axis=1)[ok] / den_b[ok]
    lo, hi = np.quantile(gains, [0.025, 0.975])
    return float(lo), float(hi)


def evaluate(samples: Sequence[Sample], hypotheses: Sequence[Hypothesis] = HYPOTHESES, *, required_years: int = 3,
             min_group: int = 30, min_train_years: int = 1, min_train: int = 200, n_boot: int = 1000,
             seed: int = 11) -> dict[str, Any]:
    folds = year_folds(samples, min_train_years=min_train_years, min_train=min_train)
    labels: dict[int, Pulse] = {}
    train_labels: dict[tuple[int, int], Pulse] = {}
    for f in folds:
        for i in f.test:
            labels[i] = pulse(samples[i].features, f.cutoffs)
        for i in f.train:
            train_labels[(f.year, i)] = pulse(samples[i].features, f.cutoffs)
    out: dict[str, Any] = {"folds": [{"test_year": f.year, "train": len(f.train), "test": len(f.test),
                                      "purged": len(f.purged), "pulse_train_samples": f.cutoffs.n_train}
                                     for f in folds],
                           "required_years": required_years, "min_group": min_group, "hypotheses": {}}
    for h in hypotheses:
        per_year: list[dict[str, Any]] = []
        pooled: dict[Any, list[tuple[bool, bool, float]]] = {}
        for f in folds:
            rows, train_rows = [], []
            for i in f.test:
                s = samples[i]
                if s.kind != h.kind:
                    continue
                r = (h.group_a(s, labels[i]), h.group_b(s, labels[i]), s.outcomes[h.outcome])
                rows.append(r)
                pooled.setdefault(s.day, []).append(r)
            for i in f.train:
                s = samples[i]
                if s.kind == h.kind:
                    p = train_labels[(f.year, i)]
                    train_rows.append((h.group_a(s, p), h.group_b(s, p), s.outcomes[h.outcome]))
            g, na, nb = _gain(rows)
            tg, _, _ = _gain(train_rows)
            per_year.append({"year": f.year, "gain": None if math.isnan(g) else round(g, 4), "n_a": na, "n_b": nb,
                             "train_gain": None if math.isnan(tg) else round(tg, 4)})
        all_rows = [r for rs in pooled.values() for r in rs]
        g_all, na_all, nb_all = _gain(all_rows)
        lo, hi = _bootstrap(pooled, n_boot=n_boot, seed=seed)
        sign = 1.0 if not h.two_sided else (1.0 if (math.isfinite(g_all) and g_all >= 0) else -1.0)
        ok_years = sum(1 for y in per_year if y["gain"] is not None and int(y["n_a"]) >= min_group
                       and int(y["n_b"]) >= min_group and float(y["gain"]) * sign > 0)
        ci_ok = math.isfinite(lo) and math.isfinite(hi) and (lo > 0 if sign > 0 else hi < 0)
        keep = ok_years >= required_years and ci_ok
        reasons = []
        if ok_years < required_years:
            reasons.append(f"right sign with enough samples in {ok_years} of {len(per_year)} test years "
                           f"(need {required_years})")
        if not ci_ok:
            reasons.append("pooled 95 % interval does not exclude zero" if math.isfinite(lo) else
                           "too few days for an interval")
        out["hypotheses"][h.name] = {
            "description": h.description, "kind": h.kind, "outcome": h.outcome, "two_sided": h.two_sided,
            "per_year": per_year, "pooled_gain": None if math.isnan(g_all) else round(g_all, 4),
            "pooled_n_a": na_all, "pooled_n_b": nb_all,
            "ci95": [None if math.isnan(lo) else round(lo, 4), None if math.isnan(hi) else round(hi, 4)],
            "stable_years": ok_years, "verdict": "KEEP" if keep else "DROP", "reasons": reasons}
    out["kept"] = [k for k, v in out["hypotheses"].items() if v["verdict"] == "KEEP"]
    return out


def render_text(rep: Mapping[str, Any]) -> list[str]:
    lines = ["Regime / Momentum Pulse: purged walk-forward, out of sample by year "
             f"(KEEP = right sign in >= {rep['required_years']} test years with >= {rep['min_group']} per group, "
             "and the pooled 95 % interval excludes zero)"]
    lines.append("folds: " + "; ".join(f"{f['test_year']}: train {f['train']:,}, test {f['test']:,}, "
                                       f"purged {f['purged']}" for f in rep["folds"]))
    for name, h in rep["hypotheses"].items():
        years = ", ".join(f"{y['year']} {y['gain']} (n {y['n_a']}/{y['n_b']})" for y in h["per_year"])
        lines.append(f"{h['verdict']:4s} {name}: pooled gain {h['pooled_gain']} CI {h['ci95']} "
                     f"(n {h['pooled_n_a']}/{h['pooled_n_b']}); by year {years}")
        lines.append(f"     {h['description']}" + (f"; {'; '.join(h['reasons'])}" if h["reasons"] else ""))
    lines.append(f"kept: {', '.join(rep['kept']) or 'none'}")
    return lines


__all__ = ["HYPOTHESES", "Fold", "Hypothesis", "evaluate", "purge_forward", "render_text", "year_folds"]
