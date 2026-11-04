"""Model registry: versioned, auditable, verifiable model artefacts.

Each registered model is a directory plus one ``model_runs`` row::

    <root>/<model_type>/<strategy_id | global>/<model_version>/
        manifest.json            everything needed to reproduce and audit the run
        evaluation_report.json   full evaluation (also embedded in the manifest)
        evaluation_report.md     human-readable report (SYNTHETIC runs say so on top)
        <kind>_..._seed<N>.txt   LightGBM text models, one per seed and head

``manifest.json`` records the six items the ml-rl-trading-safety Skill
requires before a model is deployable — feature schema, training window,
hyperparameters, environment version, evaluation report, artefact path —
plus the data source, seeds, holdout window and the sha256 of every
artefact file. ``register`` refuses an incomplete manifest. ``load`` re-checks
every hash (a modified artefact is refused) and that the database row and the
manifest agree.

Promotion rules (defence in depth; the schema enforces the same):

* A model trained on SYNTHETIC data stays TRAINED / REJECTED / ARCHIVED for
  ever. It cannot be VALIDATED or promoted, its data source cannot change,
  and a registry over the TRADING database refuses to register it at all
  (synthetic runs go to a separate research registry).
* ``mark_paper_ready`` needs a VALIDATED, HISTORICAL model whose recorded
  validation gates all passed, and a named approver.
* LIVE statuses are not reachable from the ML layer (Phase 9 promotion gate
  + explicit human approval only).
* ``production_run`` only ever returns HISTORICAL models in a deployable
  status (PAPER: PAPER_READY / LIVE_CANDIDATE / LIVE_APPROVED; LIVE:
  LIVE_APPROVED).
"""

from __future__ import annotations

import json
import platform
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm
import numpy as np
import pandas as pd

from core.clock import now_ist
from core.enums import ModelDataSource, ModelRunStatus, ModelType, TradingMode
from core.ids import new_uid
from data.storage.records import ModelRunRecord
from data.storage.repositories import ModelRunRepository
from ml.feature_engineering import FEATURE_SCHEMA_VERSION
from ml.lgbm import sha256_file
from ml.outcome_predictor import OutcomePredictor
from ml.regime_classifier import RegimeClassifier
from ml.signal_filter import SignalFilterModel

PIPELINE_VERSION = "1.0.0"
MANIFEST = "manifest.json"
REQUIRED_MANIFEST_KEYS = ("feature_schema", "training_window", "hyperparameters", "environment", "evaluation",
                          "artifact_path")

#: model kind (artefact class) <-> registry model type
KINDS: dict[str, tuple[ModelType, type[Any]]] = {
    "signal_filter": (ModelType.SIGNAL_FILTER, SignalFilterModel),
    "outcome_predictor": (ModelType.OUTCOME_PREDICTOR, OutcomePredictor),
    "regime_classifier": (ModelType.REGIME_CLASSIFIER, RegimeClassifier),
}
DEPLOYABLE: dict[TradingMode, frozenset[ModelRunStatus]] = {
    TradingMode.PAPER: frozenset({ModelRunStatus.PAPER_READY, ModelRunStatus.LIVE_CANDIDATE,
                                  ModelRunStatus.LIVE_APPROVED}),
    TradingMode.LIVE: frozenset({ModelRunStatus.LIVE_APPROVED}),
}


class RegistryError(RuntimeError):
    pass


class SyntheticModelError(RegistryError):
    """A SYNTHETIC-trained model was about to be used or promoted as production."""


def environment_info() -> dict[str, str]:
    return {"python": platform.python_version(), "lightgbm": lightgbm.__version__, "numpy": np.__version__,
            "pandas": pd.__version__, "pipeline": PIPELINE_VERSION, "feature_schema": FEATURE_SCHEMA_VERSION,
            "platform": platform.platform(terse=True)}


def environment_version(env: Mapping[str, str]) -> str:
    return (f"pipeline-{env['pipeline']}|features-{env['feature_schema']}|py-{env['python']}|"
            f"lightgbm-{env['lightgbm']}|numpy-{env['numpy']}|pandas-{env['pandas']}")


@dataclass(frozen=True)
class LoadedModel:
    record: ModelRunRecord
    manifest: dict[str, Any]
    model: Any
    directory: Path

    @property
    def is_synthetic(self) -> bool:
        return self.record.data_source is not ModelDataSource.HISTORICAL

    @property
    def kind(self) -> str:
        return str(self.manifest["model_kind"])


def _safe(part: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", part):
        raise RegistryError(f"unsafe path component {part!r}")
    return part


class ModelRegistry:
    def __init__(self, runs: ModelRunRepository, root: Path, *, allow_synthetic: bool = False,
                 now_fn: Callable[[], datetime] = now_ist) -> None:
        self.runs = runs
        self.root = Path(root)
        self.allow_synthetic = allow_synthetic
        self._now = now_fn

    # ------------------------------------------------------------------ #
    # Register
    # ------------------------------------------------------------------ #
    def new_version(self, kind: str, strategy_id: str | None, synthetic: bool) -> str:
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        return f"{kind}-{strategy_id or 'global'}-{stamp}" + ("-synthetic" if synthetic else "")

    def register(self, model: Any, *, kind: str, strategy_id: str | None, data_source: str, manifest: dict[str, Any],
                 report_md: str, status: ModelRunStatus = ModelRunStatus.TRAINED,
                 model_version: str | None = None) -> LoadedModel:
        if kind not in KINDS:
            raise RegistryError(f"unknown model kind {kind!r}")
        model_type, cls = KINDS[kind]
        if not isinstance(model, cls):
            raise RegistryError(f"{kind} must be a {cls.__name__}")
        source = ModelDataSource(data_source)
        synthetic = source is not ModelDataSource.HISTORICAL
        if synthetic and not self.allow_synthetic:
            raise SyntheticModelError("this registry only accepts models trained on HISTORICAL data; SYNTHETIC runs "
                                      "go to the research registry (never the trading database)")
        if synthetic and status not in (ModelRunStatus.TRAINED, ModelRunStatus.REJECTED):
            raise SyntheticModelError(f"a SYNTHETIC model cannot be registered as {status.value}")
        missing = [k for k in REQUIRED_MANIFEST_KEYS if k != "artifact_path" and not manifest.get(k)]
        if missing:
            raise RegistryError(f"manifest is missing {missing}: a model without them is not deployable")
        version = model_version or self.new_version(kind, strategy_id, synthetic)
        directory = self.root / _safe(model_type.value) / _safe(strategy_id or "global") / _safe(version)
        if directory.exists():
            raise RegistryError(f"{directory} already exists; model versions are immutable")
        directory.mkdir(parents=True)
        saved = model.save(directory)
        run_id = new_uid("RUN")
        window = manifest["training_window"]
        full = {**manifest, "run_id": run_id, "model_kind": kind, "model_type": model_type.value,
                "model_version": version, "strategy_id": strategy_id, "data_source": source.value,
                "synthetic": synthetic, "artifact_path": str(directory), **saved}
        (directory / "evaluation_report.json").write_text(json.dumps(manifest["evaluation"], indent=1, default=str),
                                                          encoding="utf-8")
        (directory / "evaluation_report.md").write_text(report_md, encoding="utf-8")
        full["files"] = {**full["files"], "evaluation_report.json": sha256_file(directory / "evaluation_report.json"),
                         "evaluation_report.md": sha256_file(directory / "evaluation_report.md")}
        (directory / MANIFEST).write_text(json.dumps(full, indent=1, default=str), encoding="utf-8")
        record = ModelRunRecord(
            run_id=run_id, model_type=model_type, model_version=version, strategy_id=strategy_id,
            feature_schema_version=manifest["feature_schema"]["version"],
            feature_schema_json=manifest["feature_schema"],
            training_start=datetime.fromisoformat(str(window["start"])),
            training_end=datetime.fromisoformat(str(window["end"])),
            hyperparameters_json=manifest["hyperparameters"], metrics_json=_metrics_summary(manifest["evaluation"]),
            seeds_json=list(manifest["hyperparameters"].get("seeds", [])),
            environment_version=environment_version(manifest["environment"]), artifact_path=str(directory),
            status=status, data_source=source)
        self.runs.insert(record)
        got = self.runs.get(run_id)
        assert got is not None
        return LoadedModel(got, full, model, directory)

    # ------------------------------------------------------------------ #
    # Load / verify
    # ------------------------------------------------------------------ #
    def load(self, run_id: str) -> LoadedModel:
        rec = self.runs.get(run_id)
        if rec is None:
            raise RegistryError(f"model run {run_id!r} not found")
        directory = Path(rec.artifact_path)
        try:
            manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"cannot read the manifest of {run_id}: {exc}") from exc
        for key, want in (("run_id", rec.run_id), ("model_version", rec.model_version),
                          ("data_source", rec.data_source.value), ("model_type", rec.model_type.value)):
            if manifest.get(key) != want:
                raise RegistryError(f"manifest {key} {manifest.get(key)!r} does not match the registry ({want!r})")
        if manifest["feature_schema"]["version"] != FEATURE_SCHEMA_VERSION:
            raise RegistryError(f"model uses feature schema {manifest['feature_schema']['version']}, this code "
                                f"{FEATURE_SCHEMA_VERSION}")
        for name, digest in manifest["files"].items():
            path = directory / name
            if not path.is_file() or sha256_file(path) != digest:
                raise RegistryError(f"artefact {name} of {run_id} is missing or was modified")
        _, cls = KINDS[manifest["model_kind"]]
        try:
            model = cls.load(directory, manifest)
        except Exception as exc:
            raise RegistryError(f"cannot load {run_id}: {exc}") from exc
        return LoadedModel(rec, manifest, model, directory)

    def production_run(self, model_type: ModelType, strategy_id: str | None,
                       mode: TradingMode = TradingMode.PAPER) -> ModelRunRecord | None:
        """Newest deployable HISTORICAL model for this strategy and mode (or None)."""
        allowed = DEPLOYABLE[TradingMode(mode)]
        runs = [r for r in self.runs.list(model_type, strategy_id)
                if r.status in allowed and r.data_source is ModelDataSource.HISTORICAL
                and (strategy_id is not None or r.strategy_id is None)]
        return runs[-1] if runs else None

    def load_production(self, model_type: ModelType, strategy_id: str | None,
                        mode: TradingMode = TradingMode.PAPER) -> LoadedModel | None:
        run = self.production_run(model_type, strategy_id, mode)
        if run is None:
            return None
        loaded = self.load(run.run_id)
        if loaded.is_synthetic or loaded.manifest.get("synthetic"):
            raise SyntheticModelError(f"{run.run_id} is not a HISTORICAL model")  # unreachable via the schema
        return loaded

    # ------------------------------------------------------------------ #
    # Status changes
    # ------------------------------------------------------------------ #
    def _require(self, run_id: str) -> ModelRunRecord:
        rec = self.runs.get(run_id)
        if rec is None:
            raise RegistryError(f"model run {run_id!r} not found")
        return rec

    def mark_validated(self, run_id: str) -> None:
        rec = self._require(run_id)
        if rec.data_source is not ModelDataSource.HISTORICAL:
            raise SyntheticModelError("only models trained on HISTORICAL data can be validated")
        if rec.status is not ModelRunStatus.TRAINED:
            raise RegistryError(f"only TRAINED models can be validated (is {rec.status.value})")
        if not rec.metrics_json.get("gates_passed"):
            raise RegistryError("the run's validation gates did not all pass")
        self.runs.set_status(run_id, ModelRunStatus.VALIDATED)

    def mark_paper_ready(self, run_id: str, *, approved_by: str) -> None:
        rec = self._require(run_id)
        if rec.data_source is not ModelDataSource.HISTORICAL:
            raise SyntheticModelError("a SYNTHETIC-trained model can never be promoted")
        if rec.status is not ModelRunStatus.VALIDATED:
            raise RegistryError(f"only VALIDATED models can become PAPER_READY (is {rec.status.value})")
        if not approved_by.strip():
            raise RegistryError("promotion needs a named approver")
        self.runs.set_status(run_id, ModelRunStatus.PAPER_READY, approved_by=approved_by.strip())

    def reject(self, run_id: str) -> None:
        self._require(run_id)
        self.runs.set_status(run_id, ModelRunStatus.REJECTED)

    def archive(self, run_id: str) -> None:
        self._require(run_id)
        self.runs.set_status(run_id, ModelRunStatus.ARCHIVED)

    def set_status(self, *_: Any, **__: Any) -> None:
        raise RegistryError("use mark_validated / mark_paper_ready / reject / archive; LIVE statuses are only set "
                            "by the Phase 9 promotion gate with explicit human approval")


def _metrics_summary(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    """The compact metrics stored in model_runs.metrics_json."""
    keys = ("headline", "gates_passed", "gates", "data_source", "oos_events", "holdout_events", "comparison")
    return {k: evaluation[k] for k in keys if k in evaluation}


__all__ = ["DEPLOYABLE", "KINDS", "MANIFEST", "PIPELINE_VERSION", "REQUIRED_MANIFEST_KEYS", "LoadedModel",
           "ModelRegistry", "RegistryError", "SyntheticModelError", "environment_info", "environment_version"]
