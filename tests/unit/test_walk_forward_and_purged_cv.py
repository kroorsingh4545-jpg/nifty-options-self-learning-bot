"""Chronological walk-forward splits/runner and purged K-fold CV with embargo."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from backtest.backtester import BacktestConfigError, BacktestOptions, BacktestResult, Observations
from backtest.cost_model import CostModel
from backtest.data import HistoricalDataset
from backtest.purged_cv import PurgedKFold, embargo_from_fraction
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset, trading_days
from backtest.walk_forward import WalkForwardRunner, walk_forward_splits
from config.loader import load_config
from config.settings import BotConfig
from core.clock import IST

CFG = load_config(use_default_env_file=False)
DAYS = trading_days(date(2026, 9, 1), 20, CFG.settings.market_session)


# --------------------------------------------------------------------------- #
# Splits
# --------------------------------------------------------------------------- #


def test_rolling_splits_are_chronological_with_embargo_and_holdout() -> None:
    plan = walk_forward_splits(DAYS, train_days=5, test_days=3, embargo_days=1, holdout_days=4)
    assert plan.holdout == tuple(DAYS[-4:])
    usable = DAYS[:-4]
    assert [len(f.train) for f in plan.folds] == [5] * len(plan.folds)
    for f in plan.folds:
        assert max(f.train) < min(f.embargo) < min(f.test)  # train, then embargo gap, then test
        assert len(f.embargo) == 1 and len(f.test) == 3
        assert not (set(f.train) | set(f.test)) & set(plan.holdout)
        assert list(f.train) + list(f.embargo) + list(f.test) == usable[usable.index(f.train[0]):
                                                                         usable.index(f.test[-1]) + 1]
    tests = [d for f in plan.folds for d in f.test]
    assert tests == sorted(tests) and len(tests) == len(set(tests))  # non-overlapping test windows
    assert len(plan.folds) == (len(usable) - 6) // 3


def test_anchored_splits_grow_the_training_window() -> None:
    plan = walk_forward_splits(DAYS, train_days=4, test_days=4, anchored=True)
    assert all(f.train[0] == DAYS[0] for f in plan.folds)
    assert [len(f.train) for f in plan.folds] == [4, 8, 12, 16]


@pytest.mark.parametrize("kw", [
    dict(train_days=0, test_days=1), dict(train_days=1, test_days=0), dict(train_days=15, test_days=10),
    dict(train_days=2, test_days=3, step_days=2), dict(train_days=5, test_days=5, holdout_days=12),
])
def test_impossible_or_overlapping_splits_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        walk_forward_splits(DAYS, **kw)


def test_unsorted_days_are_refused() -> None:
    with pytest.raises(ValueError, match="chronological"):
        walk_forward_splits(list(reversed(DAYS)), train_days=2, test_days=2)


# --------------------------------------------------------------------------- #
# Runner (fake backtests: fast, and record exactly which days each run saw)
# --------------------------------------------------------------------------- #


class FakeBacktests:
    """Score = the candidate's ``params.score`` on train days; records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[date, ...], float]] = []

    async def __call__(self, cfg: BotConfig, ds: HistoricalDataset, opts: BacktestOptions) -> BacktestResult:
        score = float(cfg.strategies.strategies["orb_options"].params.get("breakout_buffer_points", 0))
        self.calls.append((opts.label, tuple(ds.days()), score))
        return BacktestResult(label=opts.label, cfg=cfg, days=ds.days(), data_source=ds.source, data_description="",
                              strategies=["orb_options"], capital={"orb_options": 100_000.0}, trades=[], orders=[],
                              fills=[], executions=[], risk_events=[], daily_pnl=[], observations=Observations(),
                              cost_model=CostModel(cfg.settings.costs, slippage_ticks=1), elapsed_seconds=0.0)


@pytest.fixture(scope="module")
def ds20() -> HistoricalDataset:
    spec = SyntheticSpec(lot_units=65, start=date(2026, 9, 1), days=20, seed=1, strike_margin_points=100.0)
    return generate_synthetic_dataset(spec, CFG.settings.market_session)


async def test_runner_selects_on_train_and_scores_on_test_only(ds20: HistoricalDataset) -> None:
    fake = FakeBacktests()
    candidates = [{}, {"orb_options": {"params": {"breakout_buffer_points": 9}}},
                  {"orb_options": {"params": {"breakout_buffer_points": 9}}}]
    runner = WalkForwardRunner(CFG, ds20, candidates=candidates, backtest_fn=fake,
                               objective=lambda r: float(r.cfg.strategies.strategies["orb_options"]
                                                         .params["breakout_buffer_points"]))
    plan = walk_forward_splits(ds20.days(), train_days=6, test_days=3, embargo_days=1, holdout_days=4)
    wf = await runner.run(plan)
    assert [f.chosen for f in wf.folds] == [1] * len(plan.folds)  # tie between c1 and c2 -> earlier candidate
    for f in wf.folds:
        assert f.train_scores == [5.0, 9.0, 9.0]  # c0 = current config (buffer 5)
        assert f.test.days == list(f.fold.test)
    held = set(plan.holdout)
    for label, days, _ in fake.calls:  # every run only ever received its own window
        assert not held & set(days), label
        fold = int(label.split("-")[0].removeprefix("fold"))
        window = plan.folds[fold].train if "train" in label else plan.folds[fold].test
        assert days == window
    assert wf.holdout is None and wf.oos_days == [d for f in plan.folds for d in f.test]


async def test_holdout_is_evaluated_once_only_when_asked(ds20: HistoricalDataset) -> None:
    fake = FakeBacktests()
    plan = walk_forward_splits(ds20.days(), train_days=6, test_days=4, holdout_days=4)
    wf = await WalkForwardRunner(CFG, ds20, backtest_fn=fake, evaluate_holdout=True).run(plan)
    holdout_runs = [c for c in fake.calls if c[0] == "holdout"]
    assert len(holdout_runs) == 1 and holdout_runs[0][1] == plan.holdout
    assert wf.holdout is not None and any("single candidate" in n for n in wf.notes)
    assert not [c for c in fake.calls if "train" in c[0]]  # nothing to select with one candidate


def test_runner_refuses_risk_overrides(ds20: HistoricalDataset) -> None:
    with pytest.raises(BacktestConfigError):
        WalkForwardRunner(CFG, ds20, candidates=[{}, {"orb_options": {"risk": {"max_daily_loss": 9000}}}])
    with pytest.raises(ValueError):
        WalkForwardRunner(CFG, ds20, candidates=[])


async def test_runner_end_to_end_on_synthetic_data() -> None:
    spec = SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=3, seed=14)
    ds = generate_synthetic_dataset(spec, CFG.settings.market_session)
    plan = walk_forward_splits(ds.days(), train_days=1, test_days=1, embargo_days=0)
    wf = await WalkForwardRunner(CFG, ds, options=replace(BacktestOptions(), strategies=("orb_options",))).run(plan)
    assert len(wf.folds) == 2
    assert wf.out_of_sample.trading_days == 2
    assert all(t.created_at.date() in f.fold.test for f in wf.folds for t in f.test.trades)


# --------------------------------------------------------------------------- #
# Purged K-fold
# --------------------------------------------------------------------------- #


def _events(n: int, hold_minutes: int = 30, step_minutes: int = 10) -> tuple[list[datetime], list[datetime]]:
    t0 = [datetime(2026, 9, 1, 9, 15, tzinfo=IST) + timedelta(minutes=step_minutes * i) for i in range(n)]
    return t0, [t + timedelta(minutes=hold_minutes) for t in t0]


def test_purged_kfold_never_trains_on_labels_overlapping_the_test_period() -> None:
    t0, t1 = _events(50)
    seen: list[int] = []
    for split in PurgedKFold(5, embargo=timedelta(minutes=20)).split(t0, t1):
        test = split.test
        seen += test.tolist()
        lo, hi = t0[test[0]], max(t1[i] for i in test)
        for i in split.train:
            assert t1[i] < lo or t0[i] > hi + timedelta(minutes=20), "leak into the test period"
        assert not set(split.train) & set(test)
        assert split.purged.size > 0
        if split.fold < 4:
            assert split.embargoed.size > 0
        assert set(split.train) | set(test) | set(split.purged) | set(split.embargoed) == set(range(50))
        assert np.all(np.diff(test) == 1)  # contiguous test block
    assert sorted(seen) == list(range(50))  # every sample is tested exactly once


def test_forward_only_trains_strictly_on_the_past() -> None:
    t0, t1 = _events(40)
    splits = list(PurgedKFold(4, forward_only=True).split(t0, t1))
    assert splits[0].train.size == 0
    for s in splits[1:]:
        assert s.train.max() < s.test.min()


def test_no_embargo_and_no_overlap_keeps_everything_else() -> None:
    t0, t1 = _events(20, hold_minutes=1, step_minutes=10)
    for s in PurgedKFold(4).split(t0, t1):
        assert s.purged.size == 0 and s.embargoed.size == 0 and s.train.size == 15


@pytest.mark.parametrize("bad", ["unsorted", "end_before_start", "too_few", "length"])
def test_purged_kfold_rejects_bad_input(bad: str) -> None:
    t0, t1 = _events(10)
    if bad == "unsorted":
        t0 = list(reversed(t0))
    elif bad == "end_before_start":
        t1 = [t - timedelta(hours=1) for t in t1]
    elif bad == "too_few":
        t0, t1 = t0[:2], t1[:2]
    else:
        t1 = t1[:-1]
    with pytest.raises(ValueError):
        list(PurgedKFold(5).split(t0, t1))


def test_kfold_parameters_and_fractional_embargo() -> None:
    with pytest.raises(ValueError):
        PurgedKFold(1)
    with pytest.raises(ValueError):
        PurgedKFold(3, embargo=timedelta(minutes=-1))
    t0, _ = _events(11, step_minutes=60)
    assert embargo_from_fraction(t0, 0.1) == timedelta(hours=1)
    assert embargo_from_fraction(t0[:1], 0.1) == timedelta(0)
    with pytest.raises(ValueError):
        embargo_from_fraction(t0, 1.0)
