"""Phase 7 part 4: training pipeline, evaluation and model registry.

All training here is on SYNTHETIC data, and every synthetic model must end up
TRAINED or REJECTED in a research registry: never validated, never promoted,
never in the trading database. The HISTORICAL promotion path is tested on
bare registry rows (no trained model), because no real data exists yet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.clock import IST
from core.enums import ModelDataSource, ModelRunStatus, ModelType, TradingMode
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.records import ModelRunRecord
from data.storage.repositories import Repositories
from ml.dataset import TrainingSet, build_training_set, grid_events
from ml.evaluation import binary_metrics, multiclass_metrics, regression_metrics, roc_auc, trading_metrics
from ml.feature_store import FeatureFrame
from ml.model_registry import ModelRegistry, RegistryError, SyntheticModelError
from ml.signal_filter import SignalFilterModel
from ml.training_pipeline import TrainingConfig, TrainingError, TrainingPipeline, ValidationGates
from tests.ml_support import config, synthetic_dataset

FAST = {"num_boost_round": 40, "min_train_events": 60}


@pytest.fixture(scope="module")
def train_set() -> TrainingSet:
    cfg, ds = config(), synthetic_dataset()
    return build_training_set(cfg, ds, grid_events(ds.days(), strategy_id="orb_options", every_minutes=5),
                              strategy_id="orb_options")


def planted(ts: TrainingSet) -> TrainingSet:
    """SYNTHETIC labels with a planted rule a model can learn from the
    features at as-of: trades whose 15-minute return agrees with the trade
    direction hit the target. Still labelled SYNTHETIC."""
    f, lab = ts.features.df, ts.labels.copy()
    win = (f["ret_15m"].fillna(0) * f["direction"] > 0).to_numpy()
    lab["target_first"], lab["loss"] = win, ~win
    lab["hit"] = np.where(win, "TARGET", "STOP")
    lab["net_pnl_per_lot"] = np.where(win, 900.0, -500.0)
    lab["return_pct"] = np.where(win, 45.0, -25.0)
    return replace(ts, labels=lab)


@pytest.fixture
def research(tmp_path: Path) -> ModelRegistry:
    db = Database(tmp_path / "research.db")
    db.initialize()
    repos = Repositories(db, TradingMode.PAPER)
    sync_strategies(repos.strategies, config().strategies.strategies)
    return ModelRegistry(repos.model_runs, tmp_path / "models", allow_synthetic=True)


# --------------------------------------------------------------------------- #
# Evaluation metrics
# --------------------------------------------------------------------------- #


def test_statistical_metrics() -> None:
    y = np.array([0, 0, 1, 1])
    assert roc_auc(y, [0.1, 0.4, 0.35, 0.8]) == pytest.approx(0.75)
    assert roc_auc([1, 1], [0.2, 0.3]) is None
    m = binary_metrics(y, [0.0, 0.0, 1.0, 1.0])
    assert m["auc"] == 1.0 and m["brier"] == 0.0 and m["base_rate"] == 0.5
    mc = multiclass_metrics([0, 1, 2], np.eye(3), [1 / 3] * 3)
    assert mc["accuracy"] == 1.0 and mc["log_loss"] < mc["baseline_log_loss"]
    r = regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], 2.0)
    assert r["mae"] == 0.0 and r["r2"] == 1.0 and r["baseline_mae"] == pytest.approx(2 / 3, abs=1e-4)


def test_trading_metrics_size_like_the_risk_manager(train_set: TrainingSet) -> None:
    lab = train_set.labels.iloc[:50].reset_index(drop=True)
    cfg = config()
    kw = {"risk_per_trade": 2000.0, "no_new_entries_after": cfg.risk.time_rules.no_new_entries_after,
          "square_off_start": cfg.risk.time_rules.square_off_start}
    full = trading_metrics(lab, np.ones(50), **kw)
    lots = np.floor(2000 / ((lab["entry"] - lab["stop"]) * lab["lot_units"]))
    want = float((lab["net_pnl_per_lot"] * lots)[lots >= 1].sum())
    assert full.net_pnl == pytest.approx(want, abs=0.05) and full.trades == int((lots >= 1).sum())
    none = trading_metrics(lab, np.zeros(50), **kw)
    assert none.trades == 0 and none.expectancy is None and none.net_pnl == 0
    half = trading_metrics(lab, np.full(50, 0.5), **kw)
    assert half.trades <= full.trades  # a smaller multiplier can only shrink positions
    assert full.compliance_status in ("PASS", "PASS WITH WARNINGS")
    bad = trading_metrics(lab, np.full(50, 1.5), **kw)
    assert bad.compliance["size multiplier within [0, 1]"] == "BREACH" and bad.compliance_status == "FAIL"


# --------------------------------------------------------------------------- #
# Pipeline on SYNTHETIC data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["signal_filter", "outcome_predictor", "regime_classifier"])
def test_pipeline_on_synthetic_data_is_rejected_or_trained_never_validated(
        train_set: TrainingSet, research: ModelRegistry, kind: str) -> None:
    r = TrainingPipeline(config(), research).run(train_set, TrainingConfig(kind=kind, **FAST))  # type: ignore[arg-type]
    ev = r.evaluation
    assert ev["synthetic"] and ev["data_source"] == "SYNTHETIC" and "SYNTHETIC" in ev["warning"]
    assert r.status in (ModelRunStatus.TRAINED, ModelRunStatus.REJECTED)
    rec = r.registered.record  # type: ignore[union-attr]
    assert rec.data_source is ModelDataSource.SYNTHETIC and rec.model_version.endswith("-synthetic")
    assert rec.status is r.status and rec.seeds_json == [11, 23, 37, 41, 53]
    assert r.report_md.startswith("> **SYNTHETIC DATA")
    # every leakage-relevant split invariant
    split = ev["split"]
    assert split["forward_only"] and split["embargo_minutes"] == 60
    hold0 = min(split["holdout_days"])
    assert all(d < hold0 for d in split["development_days"])
    for f in split["folds"]:
        if "train_end" in f:
            assert f["train_end"] < f["test_start"]
    # a random walk has no edge: the signal filter must not pass its gates
    if kind != "regime_classifier":
        assert ev["oos"]["p_target"]["per_seed_auc"]["n"] == 5
        assert not ev["gates_passed"] and r.status is ModelRunStatus.REJECTED
    # never promotable
    with pytest.raises(SyntheticModelError):
        research.mark_validated(rec.run_id)
    with pytest.raises(SyntheticModelError):
        research.mark_paper_ready(rec.run_id, approved_by="a.human")
    assert research.production_run(rec.model_type, "orb_options") is None


def test_planted_signal_passes_gates_but_synthetic_stays_trained(train_set: TrainingSet,
                                                                 research: ModelRegistry) -> None:
    ts = planted(train_set)
    r = TrainingPipeline(config(), research).run(ts, TrainingConfig(kind="signal_filter", **FAST))
    ev = r.evaluation
    assert ev["oos"]["p_target"]["per_seed_auc"]["mean"] > 0.9
    t = ev["oos"]["trading"]
    assert t["filtered"]["expectancy"] > t["baseline_no_filter"]["expectancy"] > 0
    assert t["filtered"]["trades"] < t["baseline_no_filter"]["trades"]  # the filter only removes trades
    assert ev["gates_passed"], ev["gates"]
    assert r.status is ModelRunStatus.TRAINED  # passed, but SYNTHETIC: never VALIDATED
    assert r.registered.record.status is ModelRunStatus.TRAINED  # type: ignore[union-attr]
    loaded = research.load(r.registered.record.run_id)  # type: ignore[union-attr]
    X = ts.features.matrix()[:20]
    np.testing.assert_allclose(loaded.model.score(X), r.model.score(X))
    assert loaded.is_synthetic and loaded.manifest["synthetic"] is True


def test_manifest_records_everything_needed_to_deploy(train_set: TrainingSet, research: ModelRegistry) -> None:
    r = TrainingPipeline(config(), research).run(train_set, TrainingConfig(kind="signal_filter", **FAST))
    d = r.registered.directory  # type: ignore[union-attr]
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    for key in ("feature_schema", "training_window", "hyperparameters", "environment", "evaluation",
                "artifact_path", "holdout_window", "data_source", "files"):
        assert man[key], key
    assert man["feature_schema"]["version"] == "1.0.0" and len(man["feature_schema"]["features"]) == 32
    assert man["hyperparameters"]["seeds"] == [11, 23, 37, 41, 53]
    assert man["training_window"]["end"] < man["holdout_window"]["start"]
    assert {"python", "lightgbm", "numpy", "pandas", "pipeline"} <= set(man["environment"])
    rec = r.registered.record  # type: ignore[union-attr]
    assert rec.environment_version and "lightgbm" in rec.environment_version
    assert rec.training_end < datetime.fromisoformat(man["holdout_window"]["start"])
    assert (d / "evaluation_report.md").read_text(encoding="utf-8").startswith("> **SYNTHETIC")
    assert len([n for n in man["files"] if n.endswith(".txt")]) == 5  # one model file per seed


def test_tampered_artifact_is_refused(train_set: TrainingSet, research: ModelRegistry) -> None:
    r = TrainingPipeline(config(), research).run(train_set, TrainingConfig(kind="signal_filter", **FAST))
    d = r.registered.directory  # type: ignore[union-attr]
    victim = next(d.glob("signal_filter_seed*.txt"))
    victim.write_text(victim.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="modified"):
        research.load(r.registered.record.run_id)  # type: ignore[union-attr]


def test_pipeline_refuses_bad_configs(train_set: TrainingSet) -> None:
    with pytest.raises(TrainingError, match="seeds"):
        TrainingConfig(kind="signal_filter", seeds=(1, 2, 3))
    with pytest.raises(TrainingError, match="kind"):
        TrainingConfig(kind="rl_ppo")  # type: ignore[arg-type]
    with pytest.raises(TrainingError, match="holdout"):
        TrainingConfig(kind="signal_filter", holdout_fraction=0.6)
    tiny = replace(train_set, features=train_set.features.select(slice(0, 40)), labels=train_set.labels.iloc[:40])
    with pytest.raises(TrainingError):
        TrainingPipeline(config()).run(tiny, TrainingConfig(kind="signal_filter", **FAST))


def test_pipeline_refuses_misaligned_labels(train_set: TrainingSet) -> None:
    lab = train_set.labels.copy()
    lab["t0"] = lab["t0"] + pd.Timedelta(minutes=1)  # a label that does not start at its features' as-of
    with pytest.raises(TrainingError, match="leakage"):
        TrainingPipeline(config()).run(replace(train_set, labels=lab), TrainingConfig(kind="signal_filter", **FAST))


def test_gates_are_configurable(train_set: TrainingSet) -> None:
    lax = ValidationGates(min_auc=0.0, max_auc_seed_std=1.0, min_oos_events=1, min_filtered_trades=0,
                          require_positive_expectancy=False, require_beats_baseline=False, min_holdout_auc=0.0)
    r = TrainingPipeline(config()).run(planted(train_set), TrainingConfig(kind="signal_filter", gates=lax, **FAST),
                                       register=False)
    assert r.gates_passed and r.registered is None


# --------------------------------------------------------------------------- #
# Registry rules
# --------------------------------------------------------------------------- #


def test_trading_registry_refuses_synthetic_models(train_set: TrainingSet, tmp_path: Path, paper: Repositories) -> None:
    prod = ModelRegistry(paper.model_runs, tmp_path / "models")  # allow_synthetic=False: the trading registry
    with pytest.raises(SyntheticModelError, match="HISTORICAL"):
        TrainingPipeline(config(), prod).run(train_set, TrainingConfig(kind="signal_filter", **FAST))
    assert paper.model_runs.list() == []
    assert not (tmp_path / "models").exists() or not any((tmp_path / "models").rglob("*.txt"))


def test_registry_refuses_incomplete_manifests(train_set: TrainingSet, research: ModelRegistry) -> None:
    m = SignalFilterModel.fit(train_set.features.matrix(), train_set.labels["target_first"].to_numpy(),
                              num_boost_round=5)
    with pytest.raises(RegistryError, match="missing"):
        research.register(m, kind="signal_filter", strategy_id="orb_options", data_source="SYNTHETIC",
                          manifest={"feature_schema": {"version": "1.0.0"}}, report_md="")
    with pytest.raises(SyntheticModelError):
        research.register(m, kind="signal_filter", strategy_id="orb_options", data_source="SYNTHETIC",
                          manifest={}, report_md="", status=ModelRunStatus.PAPER_READY)


def _row(run_id: str, version: str, status: ModelRunStatus, source: ModelDataSource,
         metrics: dict[str, object] | None = None) -> ModelRunRecord:
    return ModelRunRecord(run_id=run_id, model_type=ModelType.SIGNAL_FILTER, model_version=version,
                          strategy_id="orb_options", feature_schema_version="1.0.0", feature_schema_json={},
                          training_start=datetime(2026, 1, 1, tzinfo=IST),
                          training_end=datetime(2026, 6, 1, tzinfo=IST), artifact_path="nowhere", status=status,
                          data_source=source,
                          metrics_json=metrics or {}, approved_by="a.human"
                          if status is ModelRunStatus.LIVE_APPROVED else None)


def test_promotion_state_machine_on_bare_rows(paper: Repositories, tmp_path: Path) -> None:
    """No model is trained here: these rows only exercise the registry's rules."""
    reg = ModelRegistry(paper.model_runs, tmp_path / "models")
    paper.model_runs.insert(_row("R-ok", "h1", ModelRunStatus.TRAINED, ModelDataSource.HISTORICAL,
                                 {"gates_passed": True}))
    paper.model_runs.insert(_row("R-fail", "h2", ModelRunStatus.TRAINED, ModelDataSource.HISTORICAL,
                                 {"gates_passed": False}))
    with pytest.raises(RegistryError, match="gates"):
        reg.mark_validated("R-fail")
    with pytest.raises(RegistryError, match="VALIDATED"):
        reg.mark_paper_ready("R-ok", approved_by="a.human")
    reg.mark_validated("R-ok")
    with pytest.raises(RegistryError, match="approver"):
        reg.mark_paper_ready("R-ok", approved_by="  ")
    assert reg.production_run(ModelType.SIGNAL_FILTER, "orb_options") is None  # VALIDATED is not deployable
    reg.mark_paper_ready("R-ok", approved_by="a.human")
    assert reg.production_run(ModelType.SIGNAL_FILTER, "orb_options").run_id == "R-ok"  # type: ignore[union-attr]
    assert reg.production_run(ModelType.SIGNAL_FILTER, "orb_options", TradingMode.LIVE) is None
    assert reg.production_run(ModelType.SIGNAL_FILTER, "vwap_ema") is None  # per-strategy isolation
    with pytest.raises(RegistryError, match="LIVE"):
        reg.set_status("R-ok", ModelRunStatus.LIVE_APPROVED)
    reg.archive("R-ok")
    assert reg.production_run(ModelType.SIGNAL_FILTER, "orb_options") is None


def test_cli_synthetic_run_never_touches_the_trading_database(config_dir: Path, edit_yaml, tmp_path: Path,  # type: ignore[no-untyped-def]
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    from ml.__main__ import main

    def point(d: dict) -> None:  # type: ignore[type-arg]
        d["ml"]["research_dir"] = str(tmp_path / "research")
        d["ml"]["artifact_dir"] = str(tmp_path / "models")
        d["database"]["path"] = str(tmp_path / "trading.db")

    edit_yaml("settings.yaml", point)
    rc = main(["train", "--config-dir", str(config_dir), "--synthetic", "--lot-units", "65", "--days", "6",
               "--strategy", "orb_options", "--kind", "signal_filter", "--folds", "3", "--start", "2026-09-01",
               "--every", "10"])
    out = capsys.readouterr().out
    assert rc == 0 and "SYNTHETIC training set" in out and "-synthetic" in out
    assert not (tmp_path / "trading.db").exists() and not (tmp_path / "models").exists()
    assert (tmp_path / "research" / "research_registry.db").is_file()
    assert any((tmp_path / "research" / "models").rglob("manifest.json"))
    assert main(["list", "--config-dir", str(config_dir), "--research"]) == 0
    assert "SYNTHETIC" in capsys.readouterr().out


def _point_cli_at(tmp_path: Path):  # type: ignore[no-untyped-def]
    def point(d: dict) -> None:  # type: ignore[type-arg]
        d["ml"]["research_dir"] = str(tmp_path / "research")
        d["ml"]["artifact_dir"] = str(tmp_path / "models")
        d["database"]["path"] = str(tmp_path / "trading.db")
    return point


@pytest.mark.parametrize("meta", [None, {"description": "no source"}, {"source": "HISTORICAL"}])
def test_cli_refuses_unlabelled_or_mislabelled_datasets(config_dir: Path, edit_yaml, tmp_path: Path,  # type: ignore[no-untyped-def]
                                                        meta: dict[str, str] | None) -> None:
    """Audit Phase 7 warning 1: a dataset's label is never guessed, and SYNTH data never trains as HISTORICAL."""
    from backtest.data import save_dataset
    from ml.__main__ import main

    edit_yaml("settings.yaml", _point_cli_at(tmp_path))
    d = save_dataset(synthetic_dataset(days=3), tmp_path / "ds")
    if meta is None:
        (d / "meta.json").unlink()
    else:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing dataset"):
        main(["train", "--config-dir", str(config_dir), "--data", str(d), "--strategy", "orb_options",
              "--kind", "signal_filter", "--folds", "3"])
    assert not (tmp_path / "trading.db").exists() and not (tmp_path / "models").exists()
    assert not (tmp_path / "research").exists()


def test_cli_routes_an_explicitly_synthetic_dataset_to_research(config_dir: Path, edit_yaml, tmp_path: Path,  # type: ignore[no-untyped-def]
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    from backtest.data import save_dataset
    from ml.__main__ import main

    edit_yaml("settings.yaml", _point_cli_at(tmp_path))
    d = save_dataset(synthetic_dataset(days=6), tmp_path / "ds")
    assert json.loads((d / "meta.json").read_text(encoding="utf-8"))["source"] == "SYNTHETIC"
    rc = main(["train", "--config-dir", str(config_dir), "--data", str(d), "--strategy", "orb_options",
               "--kind", "signal_filter", "--folds", "3", "--every", "10"])
    assert rc == 0 and "SYNTHETIC training set" in capsys.readouterr().out
    assert not (tmp_path / "trading.db").exists() and not (tmp_path / "models").exists()
    assert (tmp_path / "research" / "research_registry.db").is_file()


def test_feature_frame_used_for_training_is_tagged(train_set: TrainingSet) -> None:
    assert isinstance(train_set.features, FeatureFrame) and train_set.features.is_synthetic
