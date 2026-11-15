"""D4: purged walk-forward by year (research/regime/validate.py) on SYNTHETIC
samples with planted effects: it must keep a stable effect and drop the rest."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from backtest.purged_cv import PurgedKFold
from core.clock import IST
from research.regime.pulse import RANGE_CHOP, STRONG_TREND
from research.regime.samples import Sample
from research.regime.validate import Hypothesis, evaluate, purge_forward, year_folds

YEARS = (2022, 2023, 2024, 2025, 2026)


def make(effects: dict[int, float], *, per_year: int = 600, seed: int = 3, kind: str = "CHECKPOINT",
         noise: float = 1.0) -> list[Sample]:
    """Half the samples look like strong trends, half like chop; strong trends
    get ``effects[year]`` added to their outcome."""
    rng = np.random.default_rng(seed)
    out = []
    for y in YEARS:
        d0 = date(y, 1, 2)
        for k in range(per_year):
            day = d0 + timedelta(days=k // 4)
            t0 = datetime.combine(day, time(10, 0), tzinfo=IST) + timedelta(minutes=30 * (k % 4))
            strong = k % 2 == 0
            p = rng.uniform(0.8, 1.0) if strong else rng.uniform(0.0, 0.2)
            f = {"er": p, "adx": 10 + 40 * p, "abs_disp": 4 * p, "disp": 4 * p, "atrx": 1.0, "sd": 1e-4,
                 "ml_trend": 0.0, "ml_vol": 0.0}
            v = float(rng.normal(effects.get(y, 0.0) if strong else 0.0, noise))
            out.append(Sample(kind, day, t0, t0 + timedelta(minutes=30), 1, f, {"vol_change": "STABLE"},
                              {"cont": v, "log_rv_trailing": v, "log_rv_implied": v}))
    return sorted(out, key=lambda s: s.t0)


H = Hypothesis("planted", "strong > chop", "CHECKPOINT", "cont", lambda s, p: p.label == STRONG_TREND,
               lambda s, p: p.label == RANGE_CHOP)
H2 = Hypothesis("planted_two_sided", "strong != chop", "CHECKPOINT", "cont", lambda s, p: p.label == STRONG_TREND,
                lambda s, p: p.label == RANGE_CHOP, two_sided=True)


def verdict(samples: list[Sample], h: Hypothesis = H, **kw: object) -> dict[str, object]:
    return evaluate(samples, [h], n_boot=300, **kw)["hypotheses"][h.name]  # type: ignore[arg-type]


def test_a_stable_effect_is_kept() -> None:
    v = verdict(make(dict.fromkeys(YEARS, 0.4)))
    assert v["verdict"] == "KEEP" and v["stable_years"] == 4 and v["ci95"][0] > 0  # type: ignore[index]


def test_three_of_four_years_is_enough_two_is_not() -> None:
    assert verdict(make({2022: 0.4, 2023: 0.4, 2024: 0.4, 2025: -0.1, 2026: 0.4}))["verdict"] == "KEEP"
    v = verdict(make({2022: 0.4, 2023: 0.4, 2024: -0.4, 2025: -0.4, 2026: 0.4}))
    assert v["verdict"] == "DROP" and "2 of 4" in v["reasons"][0]  # type: ignore[index]


def test_no_effect_is_dropped() -> None:
    v = verdict(make({}))
    assert v["verdict"] == "DROP"


def test_a_one_sided_hypothesis_drops_a_reversed_effect_a_two_sided_one_keeps_it() -> None:
    s = make(dict.fromkeys(YEARS, -0.4))
    assert verdict(s)["verdict"] == "DROP"
    v = verdict(s, H2)
    assert v["verdict"] == "KEEP" and v["ci95"][1] < 0  # type: ignore[index]


def test_thin_groups_count_as_failed_years() -> None:
    v = verdict(make(dict.fromkeys(YEARS, 0.4), per_year=40), min_group=30, min_train=20)  # ~20 per group a year
    assert v["verdict"] == "DROP" and v["stable_years"] == 0


def test_train_effect_alone_never_passes() -> None:
    v = verdict(make({2022: 2.0}))  # only the first (training-only) year has the effect
    assert v["verdict"] == "DROP"


def test_folds_train_only_on_earlier_years_and_cutoffs_ignore_the_test_year() -> None:
    s = make(dict.fromkeys(YEARS, 0.4))
    folds = year_folds(s)
    assert [f.year for f in folds] == [2023, 2024, 2025, 2026]
    for f in folds:
        assert all(s[i].day.year < f.year for i in f.train) and all(s[i].day.year == f.year for i in f.test)
    changed = [x if x.day.year != 2024 else Sample(x.kind, x.day, x.t0, x.t1, x.direction,
                                                   {**x.features, "er": 0.0}, x.regime, x.outcomes) for x in s]
    a, b = year_folds(s)[1], year_folds(changed)[1]  # the 2024 fold
    assert a.cutoffs.n_train == b.cutoffs.n_train
    assert all((a.cutoffs.values[c] == b.cutoffs.values[c]).all() for c in a.cutoffs.values)


def test_purge_matches_backtest_purged_kfold_forward_only() -> None:
    s = make({}, per_year=200)
    t0, t1 = [x.t0 for x in s], [x.t1 for x in s]
    ours = {f.year: (sorted(f.train), sorted(f.test)) for f in year_folds(s, min_train=50)}
    for split in PurgedKFold(n_splits=len(YEARS), forward_only=True).split(t0, t1):
        if split.fold == 0:
            continue
        year = s[int(split.test[0])].day.year
        assert ours[year] == (sorted(split.train.tolist()), sorted(split.test.tolist()))


def test_a_training_sample_overlapping_the_test_period_is_purged() -> None:
    s = make({}, per_year=200)
    first_test = min(x.t0 for x in s if x.day.year == 2024)
    s[0] = Sample("CHECKPOINT", s[0].day, s[0].t0, first_test + timedelta(minutes=5), 1, s[0].features,
                  s[0].regime, s[0].outcomes)
    kept, purged = purge_forward(range(len(s)), s, first_test, first_test + timedelta(days=300))
    assert 0 in purged and 0 not in kept


@pytest.mark.parametrize("kind", ["ORB"])
def test_hypotheses_only_see_their_own_kind(kind: str) -> None:
    s = make(dict.fromkeys(YEARS, 0.4)) + make(dict.fromkeys(YEARS, -0.4), kind=kind, seed=8)
    s.sort(key=lambda x: x.t0)
    v = verdict(s)  # H is about CHECKPOINT samples: the ORB ones must not dilute it
    assert v["verdict"] == "KEEP"
