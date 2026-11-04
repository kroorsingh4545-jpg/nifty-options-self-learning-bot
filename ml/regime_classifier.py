"""Regime classifier: what the next ``horizon`` minutes of NIFTY are likely
to look like.

Regimes (build plan) are reported on three axes, because they are not
mutually exclusive (a day can trend up AND be highly volatile AND be an
expiry day):

* trend:       TRENDING_DOWN | RANGE_BOUND | TRENDING_UP   (ML, 3 classes)
* volatility:  LOW_VOL | NORMAL_VOL | HIGH_VOL              (ML, 3 classes)
* expiry day:  from the option calendar (deterministic, no model)

``RegimeClassifier`` is two LightGBM multiclass seed ensembles trained on
FORWARD labels (``ml.labeling.forward_regime_label``). Its predictions are a
model output only; they are never used as training features for other
models (the signal filter uses the rule-based TRAILING regime features in
``ml.feature_engineering``, which cannot leak).

``RuleBasedRegime`` reads those trailing features and is the fallback when
no regime model is loaded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ml.feature_engineering import FEATURE_NAMES
from ml.labeling import TREND_CLASSES, VOL_CLASSES
from ml.lgbm import DEFAULT_ROUNDS, DEFAULT_SEEDS, FloatArray, SeedEnsemble

MODEL_KIND = "regime_classifier"
EXPIRY_LABEL = "EXPIRY_DAY"
_EXPIRY_COL = FEATURE_NAMES.index("is_expiry_day")


@dataclass(frozen=True)
class RegimePrediction:
    trend: dict[str, float]
    vol: dict[str, float]
    expiry_day: bool
    source: str = "model"  # model | rules
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def trend_label(self) -> str:
        return max(self.trend, key=lambda k: self.trend[k])

    @property
    def vol_label(self) -> str:
        return max(self.vol, key=lambda k: self.vol[k])

    @property
    def label(self) -> str:
        parts = [self.trend_label, self.vol_label]
        if self.expiry_day:
            parts.append(EXPIRY_LABEL)
        return "|".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "trend": self.trend, "vol": self.vol, "expiry_day": self.expiry_day,
                "source": self.source}


def _probs(names: tuple[str, ...], p: np.ndarray) -> dict[str, float]:
    return {n: round(float(x), 5) for n, x in zip(names, p, strict=True)}


class RegimeClassifier:
    kind = MODEL_KIND

    def __init__(self, trend: SeedEnsemble, vol: SeedEnsemble, horizon_minutes: int) -> None:
        for m in (trend, vol):
            if m.task != "multiclass" or m.num_class != 3:
                raise ValueError("regime heads are 3-class multiclass models")
        self.trend, self.vol, self.horizon_minutes = trend, vol, horizon_minutes

    @classmethod
    def fit(cls, X: FloatArray, trend: np.ndarray, vol: np.ndarray, *, horizon_minutes: int,
            seeds: tuple[int, ...] = DEFAULT_SEEDS, params: dict[str, Any] | None = None,
            num_boost_round: int = DEFAULT_ROUNDS) -> RegimeClassifier:
        kw: dict[str, Any] = {"task": "multiclass", "num_class": 3, "seeds": seeds, "params": params,
                              "num_boost_round": num_boost_round}
        return cls(SeedEnsemble.fit(X, trend.astype(int), **kw), SeedEnsemble.fit(X, vol.astype(int), **kw),
                   horizon_minutes)

    def predict_proba(self, X: FloatArray) -> tuple[FloatArray, FloatArray]:
        return self.trend.predict(X), self.vol.predict(X)

    def predict(self, X: FloatArray) -> list[RegimePrediction]:
        X = np.atleast_2d(np.asarray(X, dtype=np.float64))
        pt, pv = self.predict_proba(X)
        return [RegimePrediction(_probs(TREND_CLASSES, pt[i]), _probs(VOL_CLASSES, pv[i]),
                                 bool(X[i, _EXPIRY_COL] == 1.0), "model",
                                 {"horizon_minutes": self.horizon_minutes}) for i in range(X.shape[0])]

    def save(self, directory: Path) -> dict[str, Any]:
        files = {**self.trend.save(directory, f"{MODEL_KIND}_trend"), **self.vol.save(directory, f"{MODEL_KIND}_vol")}
        return {"files": files, "ensembles": {"trend": self.trend.describe(), "vol": self.vol.describe()},
                "horizon_minutes": self.horizon_minutes}

    @classmethod
    def load(cls, directory: Path, manifest: dict[str, Any]) -> RegimeClassifier:
        e, f = manifest["ensembles"], manifest["files"]
        return cls(SeedEnsemble.load(directory, f"{MODEL_KIND}_trend", e["trend"], f),
                   SeedEnsemble.load(directory, f"{MODEL_KIND}_vol", e["vol"], f), int(manifest["horizon_minutes"]))


class RuleBasedRegime:
    """The trailing rule-based regime already in the feature vector."""

    kind = "regime_rules"

    @staticmethod
    def from_features(features: dict[str, float]) -> RegimePrediction:
        def one_hot(value: float, names: tuple[str, ...]) -> dict[str, float]:
            if value is None or math.isnan(value):
                return dict.fromkeys(names, round(1 / len(names), 5))
            idx = int(value) + 1  # -1/0/+1 -> 0/1/2
            return {n: 1.0 if i == idx else 0.0 for i, n in enumerate(names)}

        return RegimePrediction(one_hot(features["regime_trend"], TREND_CLASSES),
                                one_hot(features["regime_vol"], VOL_CLASSES),
                                features.get("is_expiry_day") == 1.0, "rules")


__all__ = ["EXPIRY_LABEL", "MODEL_KIND", "RegimeClassifier", "RegimePrediction", "RuleBasedRegime"]
