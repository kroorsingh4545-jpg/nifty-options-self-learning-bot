"""Outcome predictor: four heads on the same point-in-time features.

==================  =========================================  ==============
head                label (from the triple barrier)            model
==================  =========================================  ==============
p_target            target hit before stop / time barrier      binary
p_loss              net P&L per lot < 0 (after charges)        binary
exp_move_pct        gross premium move entry -> exit, %         regression
exp_holding_min     minutes from entry to the barrier          regression
==================  =========================================  ==============

Each head is a LightGBM seed ensemble. Predictions are informational (for
the dashboard, the trade journal and, in Phase 8, the RL observation); they
never size a trade or move a stop on their own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml.lgbm import DEFAULT_ROUNDS, DEFAULT_SEEDS, FloatArray, SeedEnsemble

MODEL_KIND = "outcome_predictor"

#: head -> (label column in TrainingSet.labels, task)
HEADS: dict[str, tuple[str, str]] = {
    "p_target": ("target_first", "binary"),
    "p_loss": ("loss", "binary"),
    "exp_move_pct": ("return_pct", "regression"),
    "exp_holding_min": ("holding_minutes", "regression"),
}


@dataclass(frozen=True)
class OutcomePrediction:
    p_target: float
    p_loss: float
    exp_move_pct: float
    exp_holding_min: float

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 5) for k, v in asdict(self).items()}


def head_targets(labels: pd.DataFrame) -> dict[str, np.ndarray]:
    return {h: labels[col].to_numpy().astype(np.float64) for h, (col, _) in HEADS.items()}


class OutcomePredictor:
    kind = MODEL_KIND

    def __init__(self, heads: dict[str, SeedEnsemble]) -> None:
        if set(heads) != set(HEADS):
            raise ValueError(f"outcome predictor needs heads {sorted(HEADS)}")
        for h, (_, task) in HEADS.items():
            if heads[h].task != task:
                raise ValueError(f"head {h} must be a {task} model")
        self.heads = heads

    @classmethod
    def fit(cls, X: FloatArray, labels: pd.DataFrame, *, seeds: tuple[int, ...] = DEFAULT_SEEDS,
            params: dict[str, Any] | None = None, num_boost_round: int = DEFAULT_ROUNDS) -> OutcomePredictor:
        y = head_targets(labels)
        return cls({h: SeedEnsemble.fit(X, y[h], task=task, seeds=seeds, params=params,  # type: ignore[arg-type]
                                        num_boost_round=num_boost_round)
                    for h, (_, task) in HEADS.items()})

    def predict_arrays(self, X: FloatArray) -> dict[str, FloatArray]:
        out = {h: m.predict(X) for h, m in self.heads.items()}
        for h in ("p_target", "p_loss"):
            out[h] = np.clip(out[h], 0.0, 1.0)
        out["exp_holding_min"] = np.maximum(out["exp_holding_min"], 0.0)
        return out

    def predict(self, X: FloatArray) -> list[OutcomePrediction]:
        a = self.predict_arrays(X)
        return [OutcomePrediction(float(a["p_target"][i]), float(a["p_loss"][i]), float(a["exp_move_pct"][i]),
                                  float(a["exp_holding_min"][i])) for i in range(len(a["p_target"]))]

    def save(self, directory: Path) -> dict[str, Any]:
        files: dict[str, str] = {}
        for h, m in self.heads.items():
            files.update(m.save(directory, f"{MODEL_KIND}_{h}"))
        return {"files": files, "ensembles": {h: m.describe() for h, m in self.heads.items()}}

    @classmethod
    def load(cls, directory: Path, manifest: dict[str, Any]) -> OutcomePredictor:
        return cls({h: SeedEnsemble.load(directory, f"{MODEL_KIND}_{h}", manifest["ensembles"][h], manifest["files"])
                    for h in HEADS})


__all__ = ["HEADS", "MODEL_KIND", "OutcomePrediction", "OutcomePredictor", "head_targets"]
