"""Chronological walk-forward validation.

Splits (``walk_forward_splits``) are made on trading days, in time order,
never shuffled::

    | train (rolling or anchored) | embargo | test |                 ...  | holdout |
                       | train               | embargo | test |

* ``holdout_days`` at the end are reserved as a final, untouched out-of-sample
  period: no fold ever trains or tests on them. ``WalkForwardRunner`` only
  touches them when explicitly asked (``evaluate_holdout=True``), once, with
  the candidate chosen in the last fold.
* ``embargo_days`` between each train and test window are skipped entirely.
* Test windows do not overlap; the last incomplete window is dropped.

``WalkForwardRunner`` evaluates parameter candidates (validated strategy
overrides; ``{}`` is the current config) on each TRAIN window, picks the best
by the objective (ties go to the earlier candidate, so the baseline wins
ties), then runs only that candidate on the TEST window. The out-of-sample
result is the concatenation of the test windows. Every window is a separate
backtest that only receives its own days, so nothing from a later window can
influence an earlier decision.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from backtest.backtester import Backtester, BacktestOptions, BacktestResult, with_strategy_overrides
from backtest.data import HistoricalDataset
from backtest.metrics import PerformanceMetrics, performance
from config.settings import BotConfig

Candidate = Mapping[str, Mapping[str, Any]]
Objective = Callable[[BacktestResult], float]


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train: tuple[date, ...]
    embargo: tuple[date, ...]
    test: tuple[date, ...]


@dataclass(frozen=True)
class WalkForwardPlan:
    folds: tuple[WalkForwardFold, ...]
    holdout: tuple[date, ...]
    anchored: bool
    embargo_days: int


def walk_forward_splits(days: Sequence[date], *, train_days: int, test_days: int, step_days: int | None = None,
                        embargo_days: int = 0, anchored: bool = False, holdout_days: int = 0) -> WalkForwardPlan:
    ordered = list(days)
    if ordered != sorted(set(ordered)):
        raise ValueError("days must be unique and in chronological order")
    if train_days < 1 or test_days < 1 or embargo_days < 0 or holdout_days < 0:
        raise ValueError("train_days and test_days must be >= 1; embargo_days and holdout_days >= 0")
    step = step_days or test_days
    if step < test_days:
        raise ValueError("step_days must be >= test_days (test windows may not overlap)")
    usable = ordered[:len(ordered) - holdout_days] if holdout_days else ordered
    holdout = tuple(ordered[len(usable):])
    folds: list[WalkForwardFold] = []
    test_start = train_days + embargo_days
    while test_start + test_days <= len(usable):
        train_end = test_start - embargo_days
        train = usable[0 if anchored else train_end - train_days:train_end]
        folds.append(WalkForwardFold(len(folds), tuple(train), tuple(usable[train_end:test_start]),
                                     tuple(usable[test_start:test_start + test_days])))
        test_start += step
    if not folds:
        raise ValueError(f"{len(usable)} usable days cannot fit train {train_days} + embargo {embargo_days} + "
                         f"test {test_days}")
    return WalkForwardPlan(tuple(folds), holdout, anchored, embargo_days)


def net_pnl_objective(result: BacktestResult) -> float:
    return sum(float(t.net_pnl or 0.0) for t in result.closed_trades())


@dataclass
class FoldResult:
    fold: WalkForwardFold
    chosen: int
    train_scores: list[float]
    test: BacktestResult


@dataclass
class WalkForwardResult:
    plan: WalkForwardPlan
    candidates: list[dict[str, Any]]
    folds: list[FoldResult]
    out_of_sample: PerformanceMetrics
    holdout: BacktestResult | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def oos_days(self) -> list[date]:
        return [d for f in self.folds for d in f.fold.test]


BacktestFn = Callable[[BotConfig, HistoricalDataset, BacktestOptions], Awaitable[BacktestResult]]


async def _default_backtest(cfg: BotConfig, ds: HistoricalDataset, options: BacktestOptions) -> BacktestResult:
    return await Backtester(cfg, ds, options).run()


class WalkForwardRunner:
    def __init__(self, cfg: BotConfig, dataset: HistoricalDataset, *, candidates: Sequence[Candidate] = ({},),
                 objective: Objective = net_pnl_objective, options: BacktestOptions | None = None,
                 evaluate_holdout: bool = False, backtest_fn: BacktestFn = _default_backtest) -> None:
        if not candidates:
            raise ValueError("at least one candidate (use {} for the current config)")
        self.cfg = cfg
        self.dataset = dataset
        # Validate every candidate up front (refuses capital / risk-limit overrides).
        self.configs = [with_strategy_overrides(cfg, c) for c in candidates]
        self.candidates = [dict(c) for c in candidates]
        self.objective = objective
        self.options = options or BacktestOptions()
        self.evaluate_holdout = evaluate_holdout
        self._backtest = backtest_fn

    async def _run(self, i: int, days: Sequence[date], label: str) -> BacktestResult:
        opts = replace(self.options, label=label, database_path=None)
        return await self._backtest(self.configs[i], self.dataset.slice(days), opts)

    async def run(self, plan: WalkForwardPlan) -> WalkForwardResult:
        held = set(plan.holdout)
        folds: list[FoldResult] = []
        notes: list[str] = []
        for fold in plan.folds:
            if held & (set(fold.train) | set(fold.test)):
                raise AssertionError("a fold touches the holdout")  # guaranteed by walk_forward_splits
            scores: list[float] = []
            chosen = 0
            if len(self.configs) > 1:
                for i in range(len(self.configs)):
                    res = await self._run(i, fold.train, f"fold{fold.index}-train-c{i}")
                    scores.append(self.objective(res))
                chosen = max(range(len(scores)), key=lambda i: (scores[i], -i))
            test = await self._run(chosen, fold.test, f"fold{fold.index}-test-c{chosen}")
            folds.append(FoldResult(fold, chosen, scores, test))
        if len(self.configs) == 1:
            notes.append("single candidate: no parameter selection; test windows show the current config "
                         "out of sample")
        oos_trades = [t for f in folds for t in f.test.trades]
        capital = sum(folds[0].test.capital.values()) if folds else float(self.cfg.risk.capital.total_capital)
        oos = performance(oos_trades, [d for f in folds for d in f.fold.test], capital)
        holdout = None
        if self.evaluate_holdout and plan.holdout:
            holdout = await self._run(folds[-1].chosen, plan.holdout, "holdout")
            notes.append(f"holdout evaluated once with candidate {folds[-1].chosen} (chosen in the last fold)")
        return WalkForwardResult(plan, self.candidates, folds, oos, holdout, notes)
