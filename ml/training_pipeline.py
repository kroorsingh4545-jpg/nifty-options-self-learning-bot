"""Training pipeline: leakage-safe validation, multi-seed training,
evaluation, comparison and registration.

::

    HistoricalDataset (SYNTHETIC | HISTORICAL)
        │  ml.dataset: strategy / grid events -> point-in-time features + triple-barrier labels
        ▼
    TrainingSet (sorted by t0; label end t1 per row)
        │  split by DAY: the last ``holdout_fraction`` of days = final holdout (untouched)
        ├──────────────────────────────────────────────┐
        ▼ development period                           ▼ holdout (evaluated ONCE)
    PurgedKFold(n_splits, embargo, forward_only=True)  │
      fold k: train = rows before test block k,        │
              minus rows whose [t0, t1] overlaps it    │
              (purge) and rows starting within the     │
              embargo after it                         │
      fit SeedEnsemble (>= 5 seeds) on train,          │
      predict test block (out of sample)               │
        │                                              │
        ▼                                              │
    OOS metrics per seed -> mean / std; trading        │
    metrics with the strategy's filter policy vs the   │
    no-filter baseline; risk compliance                │
        │                                              │
        ▼                                              ▼
    final fit on the whole development period (rows whose label ends
    before the holdout starts) -> holdout metrics -> comparison with the
    current production model -> validation gates
        │
        ▼
    ModelRegistry.register  (SYNTHETIC: TRAINED / REJECTED only, research registry only;
                             HISTORICAL: TRAINED -> VALIDATED when every gate passes)

Nothing is ever deployed from here: promotion to PAPER_READY needs a named
approver (``ModelRegistry.mark_paper_ready``) and LIVE needs Phase 9's
promotion gate plus explicit human approval.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from backtest.purged_cv import PurgedKFold
from config.settings import BotConfig
from core.clock import now_ist
from core.enums import ModelRunStatus
from ml.dataset import TrainingSet
from ml.evaluation import (
    binary_metrics,
    multiclass_metrics,
    regression_metrics,
    seed_spread,
    trading_metrics,
)
from ml.feature_engineering import FEATURE_NAMES, feature_schema
from ml.labeling import TREND_CLASSES, VOL_CLASSES
from ml.lgbm import DEFAULT_ROUNDS, DEFAULT_SEEDS, MIN_SEEDS, SeedEnsemble
from ml.model_registry import KINDS, LoadedModel, ModelRegistry, environment_info
from ml.outcome_predictor import HEADS, OutcomePredictor
from ml.regime_classifier import RegimeClassifier
from ml.signal_filter import FilterPolicy, SignalFilterModel

Kind = Literal["signal_filter", "outcome_predictor", "regime_classifier"]
SYNTHETIC_BANNER = ("SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real "
                    "NIFTY prices. It can never be validated, promoted or used for trading.")


class TrainingError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationGates:
    min_auc: float = 0.55  # mean OOS AUC over seeds (signal filter / outcome p_target)
    max_auc_seed_std: float = 0.05
    min_oos_events: int = 100
    min_filtered_trades: int = 30
    require_positive_expectancy: bool = True
    require_beats_baseline: bool = True  # filtered expectancy > no-filter expectancy
    min_holdout_auc: float = 0.50


@dataclass(frozen=True)
class TrainingConfig:
    kind: Kind
    seeds: tuple[int, ...] = DEFAULT_SEEDS
    n_splits: int = 5
    embargo_minutes: int = 60
    holdout_fraction: float = 0.2
    min_holdout_days: int = 1
    params: dict[str, Any] = field(default_factory=dict)
    num_boost_round: int = DEFAULT_ROUNDS
    min_train_events: int = 100
    gates: ValidationGates = ValidationGates()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise TrainingError(f"unknown model kind {self.kind!r}")
        if len(set(self.seeds)) < MIN_SEEDS:
            raise TrainingError(f"at least {MIN_SEEDS} distinct seeds are required")
        if self.n_splits < 2 or not 0 < self.holdout_fraction < 0.5 or self.embargo_minutes < 0:
            raise TrainingError("need n_splits >= 2, 0 < holdout_fraction < 0.5, embargo_minutes >= 0")

    def hyperparameters(self) -> dict[str, Any]:
        d = asdict(self)
        d["seeds"] = list(self.seeds)
        return d


@dataclass
class TrainingResult:
    kind: str
    model: Any
    evaluation: dict[str, Any]
    report_md: str
    status: ModelRunStatus
    registered: LoadedModel | None = None

    @property
    def gates_passed(self) -> bool:
        return bool(self.evaluation["gates_passed"])


# --------------------------------------------------------------------------- #
# Rows / targets per model kind
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Rows:
    X: np.ndarray
    labels: pd.DataFrame
    t0: list[datetime]
    t1: list[datetime]
    days: list[date]


def _rows(ts: TrainingSet, kind: str) -> _Rows:
    df, lab = ts.features.df, ts.labels
    if len(df) != len(lab) or not (df["event_id"].to_numpy() == lab["event_id"].to_numpy()).all():
        raise TrainingError("features and labels are not aligned")
    if not (lab["t0"] == df["as_of"]).all():
        raise TrainingError("leakage check failed: a label does not start at its feature as-of time")
    if (lab["t1"] < lab["t0"]).any():
        raise TrainingError("leakage check failed: a label ends before it starts")
    keep = np.ones(len(df), dtype=bool)
    t1 = lab["t1"]
    if kind == "regime_classifier":
        keep = lab["regime_trend"].notna().to_numpy() & ~df["as_of"].duplicated().to_numpy()
        t1 = lab["regime_t1"]
    X = df.loc[keep, list(FEATURE_NAMES)].to_numpy(dtype=np.float64)
    sel = lab.loc[keep].reset_index(drop=True)
    t0s = [t.to_pydatetime() for t in sel["t0"]]
    t1s = [t.to_pydatetime() for t in t1[keep]]
    return _Rows(X, sel, t0s, t1s, [t.date() for t in t0s])


def _fit(kind: str, X: np.ndarray, labels: pd.DataFrame, tcfg: TrainingConfig, horizon: int) -> Any:
    kw: dict[str, Any] = {"seeds": tcfg.seeds, "params": tcfg.params, "num_boost_round": tcfg.num_boost_round}
    if kind == "signal_filter":
        return SignalFilterModel.fit(X, labels["target_first"].to_numpy().astype(int), **kw)
    if kind == "outcome_predictor":
        return OutcomePredictor.fit(X, labels, **kw)
    return RegimeClassifier.fit(X, labels["regime_trend"].to_numpy().astype(int),
                                labels["regime_vol"].to_numpy().astype(int), horizon_minutes=horizon, **kw)


def _ensembles(kind: str, model: Any) -> dict[str, SeedEnsemble]:
    if kind == "signal_filter":
        return {"p_target": model.ensemble}
    if kind == "outcome_predictor":
        return dict(model.heads)
    return {"trend": model.trend, "vol": model.vol}


def _trainable(kind: str, labels: pd.DataFrame) -> str | None:
    """Why this training slice cannot be fitted (single-class labels), or None."""
    cols = {"signal_filter": ["target_first"], "outcome_predictor": ["target_first", "loss"],
            "regime_classifier": ["regime_trend", "regime_vol"]}[kind]
    for c in cols:
        if labels[c].nunique() < 2:
            return f"training labels {c} have a single class"
    return None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class TrainingPipeline:
    def __init__(self, cfg: BotConfig, registry: ModelRegistry | None = None, *,
                 now_fn: Callable[[], datetime] = now_ist) -> None:
        self.cfg = cfg
        self.registry = registry
        self._now = now_fn

    def run(self, ts: TrainingSet, tcfg: TrainingConfig, *, register: bool = True) -> TrainingResult:
        kind = tcfg.kind
        rows = _rows(ts, kind)
        scfg = self.cfg.strategies.strategies[ts.strategy_id]
        policy = FilterPolicy.from_config(scfg.ml)
        days = sorted(set(rows.days))
        n_hold = max(tcfg.min_holdout_days, math.ceil(len(days) * tcfg.holdout_fraction))
        if len(days) - n_hold < tcfg.n_splits:
            raise TrainingError(f"{len(days)} days cannot hold {n_hold} holdout days and {tcfg.n_splits} folds")
        holdout_days = days[-n_hold:]
        hold_start = min(t for t, d in zip(rows.t0, rows.days, strict=True) if d >= holdout_days[0])
        is_hold = np.array([d >= holdout_days[0] for d in rows.days])
        # Development rows: decided before the holdout AND labelled before it starts (purge).
        is_dev = np.array([(not h) and t1 < hold_start for h, t1 in zip(is_hold, rows.t1, strict=True)])
        dev_idx, hold_idx = np.flatnonzero(is_dev), np.flatnonzero(is_hold)
        if dev_idx.size < tcfg.min_train_events:
            raise TrainingError(f"only {dev_idx.size} development events (< {tcfg.min_train_events})")

        horizon = ts.regime_horizon_minutes
        heads = self._head_names(kind)
        oos = {h: [np.full(rows.X.shape[0], np.nan) for _ in tcfg.seeds] for h in heads}
        oos_proba: dict[str, list[np.ndarray]] = {h: [np.full((rows.X.shape[0], 3), np.nan) for _ in tcfg.seeds]
                                                   for h in ("trend", "vol")} if kind == "regime_classifier" else {}
        prior: dict[str, np.ndarray] = {h: np.full((rows.X.shape[0], 3), np.nan) for h in ("trend", "vol")}
        train_mean: dict[str, np.ndarray] = {h: np.full(rows.X.shape[0], np.nan) for h in heads}
        folds: list[dict[str, Any]] = []
        cv = PurgedKFold(tcfg.n_splits, embargo=timedelta(minutes=tcfg.embargo_minutes), forward_only=True)
        dev_t0 = [rows.t0[i] for i in dev_idx]
        dev_t1 = [rows.t1[i] for i in dev_idx]
        for split in cv.split(dev_t0, dev_t1):
            tr, te = dev_idx[split.train], dev_idx[split.test]
            info: dict[str, Any] = {"fold": split.fold, "train": int(tr.size), "test": int(te.size),
                                    "purged": int(split.purged.size), "embargoed": int(split.embargoed.size),
                                    "test_start": rows.t0[te[0]].isoformat(), "test_end": rows.t1[te[-1]].isoformat()}
            if tr.size:
                train_end = max(rows.t1[i] for i in tr)
                if train_end >= rows.t0[te[0]]:  # guaranteed by forward-only purging; checked anyway
                    raise TrainingError(f"leakage check failed in fold {split.fold}: a training label ends at "
                                        f"{train_end.isoformat()}, after the test block starts")
                info["train_end"] = train_end.isoformat()
            reason = None
            if tr.size < tcfg.min_train_events:
                reason = f"{tr.size} training events < {tcfg.min_train_events}"
            else:
                reason = _trainable(kind, rows.labels.iloc[tr])
            if reason:
                info["skipped"] = reason
                folds.append(info)
                continue
            model = _fit(kind, rows.X[tr], rows.labels.iloc[tr], tcfg, horizon)
            for h, ens in _ensembles(kind, model).items():
                per_seed = ens.predict_per_seed(rows.X[te])
                for s, p in enumerate(per_seed):
                    if kind == "regime_classifier":
                        oos_proba[h][s][te] = p
                    else:
                        oos[h][s][te] = p
                if kind == "regime_classifier":
                    col = "regime_trend" if h == "trend" else "regime_vol"
                    counts = np.bincount(rows.labels[col].iloc[tr].astype(int), minlength=3) + 1.0
                    prior[h][te] = counts / counts.sum()
                elif ens.task == "regression":
                    train_mean[h][te] = float(np.mean(_target(rows.labels.iloc[tr], h)))
            folds.append(info)

        scored = self._scored_mask(kind, oos, oos_proba)
        if not scored.any():
            raise TrainingError("no fold could be trained: not enough development events")
        evaluation: dict[str, Any] = {
            "model_kind": kind, "strategy_id": ts.strategy_id, "data_source": ts.data_source,
            "synthetic": ts.is_synthetic, "dataset": ts.summary(), "policy": asdict(policy),
            "split": {"development_days": [d.isoformat() for d in days[:-n_hold]],
                      "holdout_days": [d.isoformat() for d in holdout_days], "folds": folds,
                      "embargo_minutes": tcfg.embargo_minutes, "forward_only": True,
                      "development_events": int(dev_idx.size), "holdout_events": int(hold_idx.size),
                      "purged_before_holdout": int((~is_hold).sum() - dev_idx.size)},
            "oos_events": int(scored.sum()), "holdout_events": int(hold_idx.size), "seeds": list(tcfg.seeds),
        }
        if ts.is_synthetic:
            evaluation["warning"] = SYNTHETIC_BANNER
        evaluation["oos"] = self._evaluate(kind, rows, scored, oos, oos_proba, prior, train_mean, policy, ts)

        # Final model: every development row; then the untouched holdout, once.
        final = _fit(kind, rows.X[dev_idx], rows.labels.iloc[dev_idx], tcfg, horizon)
        evaluation["holdout"] = self._evaluate_model(kind, final, rows, dev_idx, hold_idx, policy, ts)
        evaluation["comparison"] = self._compare(kind, ts, rows, dev_idx, hold_idx, policy, evaluation["holdout"])
        evaluation["feature_importance"] = {h: dict(list(e.feature_importance().items())[:15])
                                            for h, e in _ensembles(kind, final).items()}
        evaluation["feature_coverage"] = ts.features.coverage()
        evaluation["leakage_checks"] = LEAKAGE_CHECKS
        gates = self._gates(kind, evaluation, tcfg.gates)
        evaluation["gates"] = gates
        evaluation["gates_passed"] = all(g["passed"] for g in gates.values())
        evaluation["headline"] = self._headline(kind, evaluation)
        status = ModelRunStatus.TRAINED if evaluation["gates_passed"] else ModelRunStatus.REJECTED
        evaluation["status"] = status.value
        if ts.is_synthetic:
            evaluation["status_note"] = ("SYNTHETIC: stays TRAINED/REJECTED for ever; never VALIDATED or promoted")
        report = render_report(evaluation)
        result = TrainingResult(kind, final, evaluation, report, status)
        if register and self.registry is not None:
            dev_t0s = [rows.t0[i] for i in dev_idx]
            dev_t1s = [rows.t1[i] for i in dev_idx]
            manifest = {
                "feature_schema": feature_schema(),
                "training_window": {"start": min(dev_t0s).isoformat(), "end": max(dev_t1s).isoformat(),
                                    "days": len({t.date() for t in dev_t0s}), "events": int(dev_idx.size)},
                "holdout_window": {"start": hold_start.isoformat(),
                                   "end": max(rows.t1[i] for i in hold_idx).isoformat() if hold_idx.size else None,
                                   "days": len(holdout_days), "events": int(hold_idx.size)},
                "hyperparameters": {**tcfg.hyperparameters(), "barrier": asdict(ts.spec),
                                    "regime_horizon_minutes": horizon, "policy": asdict(policy)},
                "environment": environment_info(),
                "evaluation": evaluation,
                "data": {"source": ts.data_source, "description": ts.features.description},
            }
            result.registered = self.registry.register(final, kind=kind, strategy_id=ts.strategy_id,
                                                       data_source=ts.data_source, manifest=manifest,
                                                       report_md=report, status=status)
            if evaluation["gates_passed"] and not ts.is_synthetic:
                self.registry.mark_validated(result.registered.record.run_id)
                result.status = ModelRunStatus.VALIDATED
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _head_names(kind: str) -> list[str]:
        if kind == "signal_filter":
            return ["p_target"]
        if kind == "outcome_predictor":
            return list(HEADS)
        return []

    @staticmethod
    def _scored_mask(kind: str, oos: dict[str, list[np.ndarray]], proba: dict[str, list[np.ndarray]]) -> np.ndarray:
        if kind == "regime_classifier":
            return np.asarray(~np.isnan(proba["trend"][0][:, 0]))
        return np.asarray(~np.isnan(next(iter(oos.values()))[0]))

    def _trading(self, labels: pd.DataFrame, score: np.ndarray, policy: FilterPolicy,
                 ts: TrainingSet) -> dict[str, Any]:
        scfg = self.cfg.strategies.strategies[ts.strategy_id]
        tr = self.cfg.risk.time_rules
        kw: dict[str, Any] = {"risk_per_trade": scfg.sizing.risk_per_trade,
                              "no_new_entries_after": tr.no_new_entries_after, "square_off_start": tr.square_off_start}
        mult = np.array([policy.decide(float(s)).size_multiplier for s in score])
        actions = pd.Series([policy.decide(float(s)).action.value for s in score]).value_counts().to_dict()
        base = trading_metrics(labels, np.ones(len(labels)), **kw)
        filt = trading_metrics(labels, mult, **kw)
        return {"baseline_no_filter": base.as_dict(), "filtered": filt.as_dict(), "actions": actions}

    def _evaluate(self, kind: str, rows: _Rows, scored: np.ndarray, oos: dict[str, list[np.ndarray]],
                  proba: dict[str, list[np.ndarray]], prior: dict[str, np.ndarray],
                  train_mean: dict[str, np.ndarray], policy: FilterPolicy, ts: TrainingSet) -> dict[str, Any]:
        lab = rows.labels.loc[scored].reset_index(drop=True)
        out: dict[str, Any] = {"events": int(scored.sum())}
        if kind == "regime_classifier":
            for h, col, names in (("trend", "regime_trend", TREND_CLASSES), ("vol", "regime_vol", VOL_CLASSES)):
                y = lab[col].astype(int).to_numpy()
                pri = prior[h][scored]
                per_seed = [multiclass_metrics(y, p[scored], pri.mean(axis=0)) for p in proba[h]]
                ens = np.mean([p[scored] for p in proba[h]], axis=0)
                out[h] = {"classes": list(names), "ensemble": multiclass_metrics(y, ens, pri.mean(axis=0)),
                          "per_seed_log_loss": seed_spread([m["log_loss"] for m in per_seed]),
                          "per_seed_accuracy": seed_spread([m["accuracy"] for m in per_seed])}
            return out
        for h in self._head_names(kind):
            y = _target(lab, h)
            seeds = [p[scored] for p in oos[h]]
            ens = np.mean(seeds, axis=0)
            if HEADS[h][1] == "binary":
                per_seed = [binary_metrics(y, p) for p in seeds]
                out[h] = {"ensemble": binary_metrics(y, ens), "per_seed_auc": seed_spread([m["auc"] for m in per_seed]),
                          "per_seed_brier": seed_spread([m["brier"] for m in per_seed])}
            else:
                mean = float(np.nanmean(train_mean[h][scored]))
                per_seed = [regression_metrics(y, p, mean) for p in seeds]
                out[h] = {"ensemble": regression_metrics(y, ens, mean),
                          "per_seed_mae": seed_spread([m["mae"] for m in per_seed])}
        ens_target = np.mean([p[scored] for p in oos["p_target"]], axis=0)
        out["trading"] = self._trading(lab, ens_target, policy, ts)
        out["trading_per_seed_expectancy"] = seed_spread([
            trading_metrics(lab, np.array([policy.decide(float(s)).size_multiplier for s in p[scored]]),
                            risk_per_trade=self.cfg.strategies.strategies[ts.strategy_id].sizing.risk_per_trade,
                            no_new_entries_after=self.cfg.risk.time_rules.no_new_entries_after,
                            square_off_start=self.cfg.risk.time_rules.square_off_start).expectancy
            for p in oos["p_target"]])
        return out

    def _evaluate_model(self, kind: str, model: Any, rows: _Rows, dev_idx: np.ndarray, hold_idx: np.ndarray,
                        policy: FilterPolicy, ts: TrainingSet) -> dict[str, Any]:
        if hold_idx.size == 0:
            return {"events": 0}
        X, lab = rows.X[hold_idx], rows.labels.iloc[hold_idx].reset_index(drop=True)
        dev_lab = rows.labels.iloc[dev_idx]
        out: dict[str, Any] = {"events": int(hold_idx.size)}
        if kind == "regime_classifier":
            pt, pv = model.predict_proba(X)
            for h, col, p in (("trend", "regime_trend", pt), ("vol", "regime_vol", pv)):
                counts = np.bincount(dev_lab[col].astype(int), minlength=3) + 1.0
                out[h] = multiclass_metrics(lab[col].astype(int).to_numpy(), p, counts / counts.sum())
            return out
        if kind == "signal_filter":
            score = model.score(X)
            out["p_target"] = binary_metrics(_target(lab, "p_target"), score)
        else:
            preds = model.predict_arrays(X)
            for h, (_, task) in HEADS.items():
                y = _target(lab, h)
                out[h] = binary_metrics(y, preds[h]) if task == "binary" else \
                    regression_metrics(y, preds[h], float(np.mean(_target(dev_lab, h))))
            score = preds["p_target"]
        out["trading"] = self._trading(lab, score, policy, ts)
        return out

    def _compare(self, kind: str, ts: TrainingSet, rows: _Rows, dev_idx: np.ndarray, hold_idx: np.ndarray,
                 policy: FilterPolicy, candidate: dict[str, Any]) -> dict[str, Any]:
        """Candidate vs the current production model on the same holdout."""
        if self.registry is None:
            return {"production": None, "note": "no registry: compared with the no-filter baseline only"}
        model_type = KINDS[kind][0]
        run = self.registry.production_run(model_type, ts.strategy_id)
        if run is None:
            return {"production": None, "note": "no production model for this strategy: compared with the "
                                                "no-filter baseline only"}
        try:
            prod = self.registry.load(run.run_id)
        except Exception as exc:
            return {"production": run.model_version, "error": f"could not load the production model: {exc}"}
        theirs = self._evaluate_model(kind, prod.model, rows, dev_idx, hold_idx, policy, ts)
        return {"production": run.model_version, "production_run_id": run.run_id, "production_holdout": theirs,
                "candidate_holdout_headline": _holdout_headline(kind, candidate),
                "production_holdout_headline": _holdout_headline(kind, theirs)}

    def _gates(self, kind: str, ev: dict[str, Any], g: ValidationGates) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}

        def gate(name: str, value: Any, threshold: Any, passed: bool) -> None:
            out[name] = {"value": value, "threshold": threshold, "passed": bool(passed)}

        oos, hold = ev["oos"], ev["holdout"]
        gate("min OOS events", ev["oos_events"], g.min_oos_events, ev["oos_events"] >= g.min_oos_events)
        if kind == "regime_classifier":
            for h in ("trend", "vol"):
                e = oos[h]["ensemble"]
                gate(f"{h}: OOS log loss below the prior baseline", e["log_loss"], e["baseline_log_loss"],
                     e["log_loss"] is not None and e["baseline_log_loss"] is not None
                     and e["log_loss"] < e["baseline_log_loss"])
            return out
        auc = oos["p_target"]["per_seed_auc"]
        gate("mean OOS AUC over seeds", auc["mean"], g.min_auc, auc["mean"] is not None and auc["mean"] >= g.min_auc)
        gate("AUC spread across seeds (std)", auc["std"], g.max_auc_seed_std,
             auc["std"] is not None and auc["std"] <= g.max_auc_seed_std)
        f, b = oos["trading"]["filtered"], oos["trading"]["baseline_no_filter"]
        gate("filtered OOS trades", f["trades"], g.min_filtered_trades, f["trades"] >= g.min_filtered_trades)
        if g.require_positive_expectancy:
            gate("filtered OOS expectancy > 0 (net of costs)", f["expectancy"], 0,
                 f["expectancy"] is not None and f["expectancy"] > 0)
        if g.require_beats_baseline:
            gate("filtered OOS expectancy > no-filter baseline", f["expectancy"], b["expectancy"],
                 f["expectancy"] is not None and b["expectancy"] is not None and f["expectancy"] > b["expectancy"])
        gate("OOS risk compliance (no BREACH)", f["compliance_status"], "PASS / PASS WITH WARNINGS",
             f["compliance_status"] != "FAIL")
        h_auc = hold.get("p_target", {}).get("auc")
        gate("holdout AUC", h_auc, g.min_holdout_auc, h_auc is not None and h_auc >= g.min_holdout_auc)
        h_trade = hold.get("trading", {}).get("filtered", {})
        gate("holdout risk compliance (no BREACH)", h_trade.get("compliance_status"), "PASS / PASS WITH WARNINGS",
             h_trade.get("compliance_status") not in (None, "FAIL"))
        return out

    @staticmethod
    def _headline(kind: str, ev: dict[str, Any]) -> dict[str, Any]:
        oos = ev["oos"]
        if kind == "regime_classifier":
            return {"oos_trend_log_loss": oos["trend"]["ensemble"]["log_loss"],
                    "oos_trend_baseline": oos["trend"]["ensemble"]["baseline_log_loss"],
                    "oos_vol_log_loss": oos["vol"]["ensemble"]["log_loss"],
                    "oos_vol_baseline": oos["vol"]["ensemble"]["baseline_log_loss"],
                    "holdout": _holdout_headline(kind, ev["holdout"]), "gates_passed": ev["gates_passed"]}
        t = oos["trading"]
        return {"oos_auc_mean": oos["p_target"]["per_seed_auc"]["mean"],
                "oos_auc_std": oos["p_target"]["per_seed_auc"]["std"],
                "oos_filtered_expectancy": t["filtered"]["expectancy"],
                "oos_baseline_expectancy": t["baseline_no_filter"]["expectancy"],
                "oos_filtered_trades": t["filtered"]["trades"],
                "holdout": _holdout_headline(kind, ev["holdout"]), "gates_passed": ev["gates_passed"]}


def _target(labels: pd.DataFrame, head: str) -> np.ndarray:
    col = HEADS[head][0]
    return labels[col].to_numpy().astype(np.float64)


def _holdout_headline(kind: str, hold: dict[str, Any]) -> dict[str, Any]:
    if not hold.get("events"):
        return {"events": 0}
    if kind == "regime_classifier":
        return {"events": hold["events"], "trend_log_loss": hold["trend"]["log_loss"],
                "vol_log_loss": hold["vol"]["log_loss"]}
    t = hold["trading"]
    return {"events": hold["events"], "auc": hold["p_target"]["auc"], "brier": hold["p_target"]["brier"],
            "filtered_expectancy": t["filtered"]["expectancy"],
            "baseline_expectancy": t["baseline_no_filter"]["expectancy"], "filtered_trades": t["filtered"]["trades"]}


LEAKAGE_CHECKS = [
    "features computed by compute_features from bars completed at or before as-of (Bars.upto); "
    "verified by tests that rewrite every later bar and get identical features",
    "every label starts at its feature as-of time (t0 == as_of, asserted) and ends at or after it (t1 >= t0)",
    "recent-outcome features use only labels known by as-of (t1 <= as_of)",
    "regime features are trailing rules; the forward regime model is never a training feature",
    "no dataset-wide scaling or normalisation (no scaler to fit, nothing to leak)",
    "chronological split; never shuffled",
    "forward-only purged K-fold: training rows whose [t0, t1] overlaps a test block are purged, rows starting "
    "within the embargo after it are dropped, rows after it are never used",
    "final holdout = last days, untouched until the single final evaluation; development rows whose label ends "
    "after the holdout starts are purged",
]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 100 else f"{v:,.2f}"
    return str(v)


def render_report(ev: dict[str, Any]) -> str:
    lines: list[str] = []
    if ev.get("synthetic"):
        lines += [f"> **{SYNTHETIC_BANNER}**", ""]
    lines += [f"# {ev['model_kind']} - {ev['strategy_id']}", "",
              f"* data source: **{ev['data_source']}**", f"* status: **{ev['status']}**"
              + (f" ({ev['status_note']})" if ev.get("status_note") else ""),
              f"* events: {ev['dataset']['events']} (OOS scored {ev['oos_events']}, holdout {ev['holdout_events']})",
              f"* seeds: {ev['seeds']}", f"* development days: {len(ev['split']['development_days'])}, holdout days: "
              f"{', '.join(ev['split']['holdout_days'])}", "", "## Validation folds (forward-only, purged, embargoed)",
              "", "| fold | train | test | purged | embargoed | test window | note |", "|---|---|---|---|---|---|---|"]
    for f in ev["split"]["folds"]:
        lines.append(f"| {f['fold']} | {f['train']} | {f['test']} | {f['purged']} | {f['embargoed']} | "
                     f"{f['test_start'][:16]} -> {f['test_end'][:16]} | {f.get('skipped', '')} |")
    lines += ["", "## Headline", "", "| metric | value |", "|---|---|"]
    for k, v in ev["headline"].items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                lines.append(f"| holdout {k2} | {_fmt(v2)} |")
        else:
            lines.append(f"| {k} | {_fmt(v)} |")
    if "trading" in ev["oos"]:
        lines += ["", "## Trading metrics (event level, net of charges and slippage)", "",
                  "| set | trades | win rate % | profit factor | expectancy | net P&L | max DD | avg hold min | "
                  "compliance |", "|---|---|---|---|---|---|---|---|---|"]
        for name, block in (("OOS", ev["oos"]["trading"]), ("holdout", ev["holdout"].get("trading"))):
            if not block:
                continue
            for which in ("baseline_no_filter", "filtered"):
                t = block[which]
                lines.append(f"| {name} {which} | {t['trades']} | {_fmt(t['win_rate_pct'])} | "
                             f"{_fmt(t['profit_factor'])} | {_fmt(t['expectancy'])} | {_fmt(t['net_pnl'])} | "
                             f"{_fmt(t['max_drawdown'])} | {_fmt(t['avg_holding_minutes'])} | "
                             f"{t['compliance_status']} |")
    lines += ["", "## Validation gates", "", "| gate | value | threshold | passed |", "|---|---|---|---|"]
    for name, g in ev["gates"].items():
        lines.append(f"| {name} | {_fmt(g['value'])} | {_fmt(g['threshold'])} | {'yes' if g['passed'] else 'NO'} |")
    comp = ev.get("comparison", {})
    lines += ["", "## Comparison with production", "",
              comp.get("note") or f"production {comp.get('production')}: {comp.get('production_holdout_headline')}",
              "", "## Leakage checks", "", *[f"* {c}" for c in ev["leakage_checks"]], ""]
    return "\n".join(lines)


__all__ = ["LEAKAGE_CHECKS", "SYNTHETIC_BANNER", "TrainingConfig", "TrainingError", "TrainingPipeline",
           "TrainingResult", "ValidationGates", "render_report"]
