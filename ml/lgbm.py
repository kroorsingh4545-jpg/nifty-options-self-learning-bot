"""LightGBM seed ensembles: the one model primitive behind every ML model.

Every model is trained once per random seed (minimum 5, per the
ml-rl-trading-safety Skill) with LightGBM's bagging and feature subsampling
on, so the seeds genuinely differ. Predictions are the MEAN over the seeds;
no seed is ever picked as "the best". Training is deterministic for a given
seed (``deterministic=True``, single thread), so a run can be reproduced
exactly from its manifest.

Artefacts are LightGBM text model files (no pickle), one per seed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import numpy.typing as npt

from ml.feature_engineering import FEATURE_NAMES

Task = Literal["binary", "multiclass", "regression"]
FloatArray = npt.NDArray[np.float64]
MIN_SEEDS = 5
DEFAULT_SEEDS: tuple[int, ...] = (11, 23, 37, 41, 53)

BASE_PARAMS: dict[str, Any] = {
    "learning_rate": 0.05,
    "num_leaves": 15,
    "max_depth": -1,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
}
DEFAULT_ROUNDS = 150


class ModelTrainingError(ValueError):
    pass


def _objective(task: Task, num_class: int) -> dict[str, Any]:
    if task == "binary":
        return {"objective": "binary", "metric": "binary_logloss"}
    if task == "multiclass":
        return {"objective": "multiclass", "num_class": num_class, "metric": "multi_logloss"}
    return {"objective": "regression", "metric": "l2"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class SeedEnsemble:
    task: Task
    seeds: tuple[int, ...]
    params: dict[str, Any]
    num_boost_round: int
    boosters: list[lgb.Booster] = field(default_factory=list)
    num_class: int = 1
    feature_names: tuple[str, ...] = FEATURE_NAMES

    @classmethod
    def fit(cls, X: FloatArray, y: npt.NDArray[Any], *, task: Task, seeds: Sequence[int] = DEFAULT_SEEDS,
            params: Mapping[str, Any] | None = None, num_boost_round: int = DEFAULT_ROUNDS, num_class: int = 1,
            min_seeds: int = MIN_SEEDS) -> SeedEnsemble:
        seeds = tuple(int(s) for s in seeds)
        if len(set(seeds)) < min_seeds:
            raise ModelTrainingError(f"at least {min_seeds} distinct seeds are required (got {len(set(seeds))})")
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y)
        if X.ndim != 2 or X.shape[1] != len(FEATURE_NAMES):
            raise ModelTrainingError(f"X must have {len(FEATURE_NAMES)} feature columns (schema order)")
        if X.shape[0] != y.shape[0] or X.shape[0] == 0:
            raise ModelTrainingError("X and y must be non-empty and the same length")
        if task in ("binary", "multiclass") and np.unique(y).size < 2:
            raise ModelTrainingError("the training labels contain a single class; nothing to learn")
        if task == "regression" and not np.all(np.isfinite(y.astype(np.float64))):
            raise ModelTrainingError("regression targets must be finite")
        merged = {**BASE_PARAMS, **dict(params or {}), **_objective(task, num_class)}
        out = cls(task, seeds, {k: v for k, v in merged.items() if k != "seed"}, num_boost_round,
                  num_class=num_class)
        label = y.astype(np.float64)
        for s in seeds:
            ds = lgb.Dataset(X, label=label, feature_name=list(FEATURE_NAMES), free_raw_data=False,
                             params={"verbose": -1})
            p = {**out.params, "seed": s, "bagging_seed": s, "feature_fraction_seed": s, "data_random_seed": s}
            out.boosters.append(lgb.train(p, ds, num_boost_round=num_boost_round))
        return out

    # ------------------------------------------------------------------ #
    def _check(self, X: FloatArray) -> FloatArray:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.shape[1] != len(self.feature_names):
            raise ValueError(f"expected {len(self.feature_names)} features, got {X.shape[1]}")
        return X

    def predict_per_seed(self, X: FloatArray) -> list[FloatArray]:
        X = self._check(X)
        return [np.asarray(b.predict(X), dtype=np.float64) for b in self.boosters]

    def predict(self, X: FloatArray) -> FloatArray:
        """Mean over seeds: P(class 1) for binary, class probabilities
        (n, num_class) for multiclass, values for regression."""
        preds = self.predict_per_seed(X)
        return np.asarray(np.mean(np.stack(preds), axis=0), dtype=np.float64)

    def feature_importance(self) -> dict[str, float]:
        gains = np.mean([b.feature_importance(importance_type="gain") for b in self.boosters], axis=0)
        total = float(np.sum(gains)) or 1.0
        return {n: round(float(g) / total, 5) for n, g in sorted(zip(self.feature_names, gains, strict=True),
                                                                 key=lambda kv: -kv[1])}

    # ------------------------------------------------------------------ #
    def save(self, directory: Path, prefix: str) -> dict[str, str]:
        """One text model file per seed; returns {file name: sha256}."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        out = {}
        for s, b in zip(self.seeds, self.boosters, strict=True):
            path = directory / f"{prefix}_seed{s}.txt"
            path.write_text(b.model_to_string(), encoding="utf-8")
            out[path.name] = sha256_file(path)
        return out

    def describe(self) -> dict[str, Any]:
        return {"task": self.task, "seeds": list(self.seeds), "params": self.params,
                "num_boost_round": self.num_boost_round, "num_class": self.num_class,
                "feature_names": list(self.feature_names)}

    @classmethod
    def load(cls, directory: Path, prefix: str, meta: Mapping[str, Any], hashes: Mapping[str, str]) -> SeedEnsemble:
        """Load and verify every seed file against the recorded sha256."""
        directory = Path(directory)
        if tuple(meta["feature_names"]) != FEATURE_NAMES:
            raise ModelTrainingError("model feature names do not match the current feature schema")
        boosters = []
        for s in meta["seeds"]:
            path = directory / f"{prefix}_seed{s}.txt"
            want = hashes.get(path.name)
            if want is None or sha256_file(path) != want:
                raise ModelTrainingError(f"artifact {path.name} is missing from the manifest or was modified")
            boosters.append(lgb.Booster(model_str=path.read_text(encoding="utf-8")))
        return cls(meta["task"], tuple(meta["seeds"]), dict(meta["params"]), int(meta["num_boost_round"]),
                   boosters, int(meta["num_class"]), tuple(meta["feature_names"]))


__all__ = ["BASE_PARAMS", "DEFAULT_ROUNDS", "DEFAULT_SEEDS", "MIN_SEEDS", "ModelTrainingError", "SeedEnsemble",
           "Task", "sha256_file"]
