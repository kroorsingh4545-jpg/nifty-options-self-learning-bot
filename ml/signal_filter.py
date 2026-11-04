"""ML signal filter: P(target before stop) for a strategy signal, and the
policy that turns that score into a decision.

Score:  ``SignalFilterModel`` — a LightGBM seed ensemble (binary) trained on
        triple-barrier labels (1 = the target was hit before the stop or the
        time barrier). The score is a probability in [0, 1].

Policy (``FilterPolicy``), per strategy, from ``strategies.<id>.ml``:

    score >= reduce_below               ALLOW   full (risk-sized) size
    threshold <= score < reduce_below   REDUCE  size x min_size_multiplier ... 1.0 (linear)
    threshold - delay_band <= score < threshold
                                        DELAY   veto now; the strategy may
                                                signal again on a later candle
    otherwise                           REJECT

Default threshold: 0.60 (build plan). What the filter can NEVER do, by
construction: raise size above the risk-sized quantity (multiplier is in
[0, 1], and ``core.interfaces.FilterDecision`` refuses anything else), move
or remove a stop-loss or target (it has no access to them), or bypass the
risk gate (its decision goes INTO ``RiskManager``, which re-checks every
limit). A missing or unusable score fails closed (REJECT).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from config.settings import MLConfig
from ml.lgbm import DEFAULT_ROUNDS, DEFAULT_SEEDS, FloatArray, SeedEnsemble

MODEL_KIND = "signal_filter"


class FilterAction(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    DELAY = "DELAY"
    REJECT = "REJECT"


@dataclass(frozen=True)
class FilterVerdict:
    action: FilterAction
    score: float | None
    size_multiplier: float
    reason: str

    @property
    def allow(self) -> bool:
        return self.action in (FilterAction.ALLOW, FilterAction.REDUCE)

    def __post_init__(self) -> None:
        if not 0.0 <= self.size_multiplier <= 1.0:
            raise ValueError("size_multiplier must be within [0, 1]: the filter can only reduce size")
        if not self.allow and self.size_multiplier != 0.0:
            raise ValueError("a vetoed signal has size 0")


@dataclass(frozen=True)
class FilterPolicy:
    threshold: float = 0.60
    reduce_below: float = 0.70
    min_size_multiplier: float = 0.5
    delay_band: float = 0.05

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= self.reduce_below <= 1.0:
            raise ValueError("need 0 <= threshold <= reduce_below <= 1")
        if not 0.0 < self.min_size_multiplier <= 1.0:
            raise ValueError("min_size_multiplier must be in (0, 1]")
        if not 0.0 <= self.delay_band <= self.threshold:
            raise ValueError("delay_band must be in [0, threshold]")

    @classmethod
    def from_config(cls, ml: MLConfig) -> FilterPolicy:
        return cls(ml.confidence_threshold, ml.reduce_below, ml.min_size_multiplier, ml.delay_band)

    def decide(self, score: float | None) -> FilterVerdict:
        if score is None or not math.isfinite(score) or not 0.0 <= score <= 1.0:
            return FilterVerdict(FilterAction.REJECT, None, 0.0, "no usable ML score (fail-closed)")
        s = float(score)
        if s >= self.reduce_below:
            return FilterVerdict(FilterAction.ALLOW, s, 1.0, f"score {s:.3f} >= {self.reduce_below:.2f}")
        if s >= self.threshold:
            span = self.reduce_below - self.threshold
            frac = (s - self.threshold) / span if span > 0 else 1.0
            mult = round(self.min_size_multiplier + (1.0 - self.min_size_multiplier) * frac, 4)
            return FilterVerdict(FilterAction.REDUCE, s, min(1.0, mult),
                                 f"score {s:.3f} in [{self.threshold:.2f}, {self.reduce_below:.2f}): size x{mult:.2f}")
        if self.delay_band > 0 and s >= self.threshold - self.delay_band:
            return FilterVerdict(FilterAction.DELAY, s, 0.0,
                                 f"score {s:.3f} just below {self.threshold:.2f}: delayed (re-evaluate next signal)")
        return FilterVerdict(FilterAction.REJECT, s, 0.0, f"score {s:.3f} < {self.threshold:.2f}")


class SignalFilterModel:
    """P(target before stop) from point-in-time features."""

    kind = MODEL_KIND

    def __init__(self, ensemble: SeedEnsemble) -> None:
        if ensemble.task != "binary":
            raise ValueError("the signal filter is a binary model")
        self.ensemble = ensemble

    @classmethod
    def fit(cls, X: FloatArray, target_first: np.ndarray, *, seeds: tuple[int, ...] = DEFAULT_SEEDS,
            params: dict[str, Any] | None = None, num_boost_round: int = DEFAULT_ROUNDS) -> SignalFilterModel:
        return cls(SeedEnsemble.fit(X, target_first.astype(int), task="binary", seeds=seeds, params=params,
                                    num_boost_round=num_boost_round))

    def score(self, X: FloatArray) -> FloatArray:
        return np.clip(self.ensemble.predict(X), 0.0, 1.0)

    def save(self, directory: Path) -> dict[str, Any]:
        return {"files": self.ensemble.save(directory, MODEL_KIND), "ensembles": {MODEL_KIND: self.ensemble.describe()}}

    @classmethod
    def load(cls, directory: Path, manifest: dict[str, Any]) -> SignalFilterModel:
        return cls(SeedEnsemble.load(directory, MODEL_KIND, manifest["ensembles"][MODEL_KIND], manifest["files"]))


__all__ = ["MODEL_KIND", "FilterAction", "FilterPolicy", "FilterVerdict", "SignalFilterModel"]
