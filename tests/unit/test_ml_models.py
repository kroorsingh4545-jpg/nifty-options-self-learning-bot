"""Phase 7 part 3: LightGBM seed ensembles, signal filter, outcome predictor,
regime classifier (SYNTHETIC feature matrices only)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from config.settings import MLConfig
from core.interfaces import FilterDecision
from ml.feature_engineering import FEATURE_NAMES
from ml.labeling import TREND_CLASSES, VOL_CLASSES
from ml.lgbm import ModelTrainingError, SeedEnsemble
from ml.outcome_predictor import HEADS, OutcomePredictor
from ml.regime_classifier import RegimeClassifier, RuleBasedRegime
from ml.signal_filter import FilterAction, FilterPolicy, FilterVerdict, SignalFilterModel

N = len(FEATURE_NAMES)
COL = {n: i for i, n in enumerate(FEATURE_NAMES)}
SEEDS = (1, 2, 3, 4, 5)


def planted(n: int = 1200, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """SYNTHETIC features with a planted, learnable rule: the target is hit
    when the 15-minute return plus half the opening-range position is positive."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, N))
    X[:, COL["direction"]] = rng.choice([-1.0, 1.0], size=n)
    X[rng.random((n, N)) < 0.05] = np.nan  # some missing values
    signal = np.nan_to_num(X[:, COL["ret_15m"]]) + 0.5 * np.nan_to_num(X[:, COL["or_position"]])
    y = (signal + rng.normal(scale=0.5, size=n) > 0).astype(int)
    return X, y


def auc(y: np.ndarray, s: np.ndarray) -> float:
    ranks = pd.Series(s).rank().to_numpy()
    pos = y == 1
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum()))


# --------------------------------------------------------------------------- #
# Seed ensembles
# --------------------------------------------------------------------------- #


def test_ensemble_learns_a_planted_signal_out_of_sample() -> None:
    X, y = planted()
    m = SeedEnsemble.fit(X[:900], y[:900], task="binary", seeds=SEEDS, num_boost_round=80)
    assert auc(y[900:], m.predict(X[900:])) > 0.8
    imp = m.feature_importance()
    assert next(iter(imp)) == "ret_15m" and math.isclose(sum(imp.values()), 1.0, abs_tol=1e-3)


def test_ensemble_requires_five_seeds_and_two_classes() -> None:
    X, y = planted(200)
    with pytest.raises(ModelTrainingError, match="5 distinct seeds"):
        SeedEnsemble.fit(X, y, task="binary", seeds=(1, 2, 3, 4))
    with pytest.raises(ModelTrainingError, match="5 distinct seeds"):
        SeedEnsemble.fit(X, y, task="binary", seeds=(1, 1, 2, 3, 4))
    with pytest.raises(ModelTrainingError, match="single class"):
        SeedEnsemble.fit(X, np.zeros(200), task="binary", seeds=SEEDS)
    with pytest.raises(ModelTrainingError, match="feature columns"):
        SeedEnsemble.fit(X[:, :5], y, task="binary", seeds=SEEDS)
    with pytest.raises(ModelTrainingError, match="finite"):
        SeedEnsemble.fit(X, np.full(200, np.nan), task="regression", seeds=SEEDS)


def test_training_is_deterministic_per_seed_and_seeds_differ() -> None:
    X, y = planted(600)
    a = SeedEnsemble.fit(X, y, task="binary", seeds=SEEDS, num_boost_round=40)
    b = SeedEnsemble.fit(X, y, task="binary", seeds=SEEDS, num_boost_round=40)
    np.testing.assert_array_equal(a.predict(X), b.predict(X))
    per_seed = a.predict_per_seed(X)
    assert any(not np.array_equal(per_seed[0], p) for p in per_seed[1:])
    np.testing.assert_allclose(a.predict(X), np.mean(per_seed, axis=0))


def test_save_load_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    X, y = planted(400)
    m = SeedEnsemble.fit(X, y, task="binary", seeds=SEEDS, num_boost_round=30)
    hashes = m.save(tmp_path, "sf")
    back = SeedEnsemble.load(tmp_path, "sf", m.describe(), hashes)
    np.testing.assert_allclose(back.predict(X), m.predict(X))
    victim = tmp_path / "sf_seed3.txt"
    victim.write_text(victim.read_text(encoding="utf-8").replace("Tree=0", "Tree=0 ", 1), encoding="utf-8")
    with pytest.raises(ModelTrainingError, match="modified"):
        SeedEnsemble.load(tmp_path, "sf", m.describe(), hashes)
    with pytest.raises(ModelTrainingError, match="feature schema"):
        SeedEnsemble.load(tmp_path, "sf", {**m.describe(), "feature_names": ["a"]}, hashes)


def test_predict_checks_the_feature_count() -> None:
    X, y = planted(300)
    m = SeedEnsemble.fit(X, y, task="binary", seeds=SEEDS, num_boost_round=10)
    assert m.predict(X[0]).shape == (1,)
    with pytest.raises(ValueError):
        m.predict(X[:, :3])


# --------------------------------------------------------------------------- #
# Signal filter policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("score", "action", "mult"), [
    (0.95, FilterAction.ALLOW, 1.0), (0.70, FilterAction.ALLOW, 1.0), (0.65, FilterAction.REDUCE, 0.75),
    (0.60, FilterAction.REDUCE, 0.5), (0.58, FilterAction.DELAY, 0.0), (0.55, FilterAction.DELAY, 0.0),
    (0.54, FilterAction.REJECT, 0.0), (0.0, FilterAction.REJECT, 0.0),
])
def test_filter_policy_bands(score: float, action: FilterAction, mult: float) -> None:
    v = FilterPolicy().decide(score)
    assert v.action is action and v.size_multiplier == pytest.approx(mult)
    assert v.allow is (action in (FilterAction.ALLOW, FilterAction.REDUCE))


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -0.1, 1.1])
def test_filter_policy_fails_closed(bad: float | None) -> None:
    v = FilterPolicy().decide(bad)
    assert v.action is FilterAction.REJECT and v.size_multiplier == 0.0 and v.score is None


def test_filter_can_never_increase_size() -> None:
    p = FilterPolicy(threshold=0.5, reduce_below=0.9, min_size_multiplier=0.2, delay_band=0.0)
    for s in np.linspace(0, 1, 501):
        v = p.decide(float(s))
        assert 0.0 <= v.size_multiplier <= 1.0
        FilterDecision(allow=v.allow, score=v.score, size_multiplier=v.size_multiplier)  # the engine's contract
    with pytest.raises(ValueError):
        FilterVerdict(FilterAction.ALLOW, 0.9, 1.5, "")
    with pytest.raises(ValueError):
        FilterVerdict(FilterAction.REJECT, 0.1, 0.5, "")


def test_filter_policy_from_config_and_validation() -> None:
    p = FilterPolicy.from_config(MLConfig())
    assert (p.threshold, p.reduce_below, p.min_size_multiplier, p.delay_band) == (0.60, 0.70, 0.5, 0.05)
    with pytest.raises(ValueError):
        MLConfig(confidence_threshold=0.7, reduce_below=0.6)
    with pytest.raises(ValueError):
        MLConfig(confidence_threshold=0.03, delay_band=0.05)
    with pytest.raises(ValueError):
        MLConfig(min_size_multiplier=0.0)
    with pytest.raises(ValueError):
        FilterPolicy(threshold=0.8, reduce_below=0.7)


def test_signal_filter_model_scores_in_unit_interval(tmp_path: Path) -> None:
    X, y = planted(600)
    m = SignalFilterModel.fit(X, y, seeds=SEEDS, num_boost_round=30)
    s = m.score(X)
    assert s.min() >= 0 and s.max() <= 1
    man = m.save(tmp_path)
    np.testing.assert_allclose(SignalFilterModel.load(tmp_path, man).score(X), s)


# --------------------------------------------------------------------------- #
# Outcome predictor
# --------------------------------------------------------------------------- #


def test_outcome_predictor_heads(tmp_path: Path) -> None:
    X, y = planted(800)
    rng = np.random.default_rng(1)
    labels = pd.DataFrame({"target_first": y.astype(bool), "loss": (1 - y).astype(bool),
                           "return_pct": np.where(y == 1, 40.0, -20.0) + rng.normal(size=800),
                           "holding_minutes": rng.uniform(1, 60, size=800)})
    m = OutcomePredictor.fit(X[:600], labels.iloc[:600], seeds=SEEDS, num_boost_round=40)
    preds = m.predict(X[600:])
    assert len(preds) == 200 and set(preds[0].as_dict()) == set(HEADS)
    a = m.predict_arrays(X[600:])
    assert auc(y[600:], a["p_target"]) > 0.75 and auc(1 - y[600:], a["p_loss"]) > 0.75
    assert np.corrcoef(a["exp_move_pct"], y[600:])[0, 1] > 0.5
    assert (a["exp_holding_min"] >= 0).all()
    man = m.save(tmp_path)
    np.testing.assert_allclose(OutcomePredictor.load(tmp_path, man).predict_arrays(X)["p_loss"],
                               m.predict_arrays(X)["p_loss"])


# --------------------------------------------------------------------------- #
# Regime classifier
# --------------------------------------------------------------------------- #


def test_regime_classifier(tmp_path: Path) -> None:
    X, _ = planted(900)
    trend = np.digitize(np.nan_to_num(X[:, COL["ret_30m"]]), [-0.5, 0.5])  # 0/1/2
    vol = np.digitize(np.nan_to_num(X[:, COL["rv_30m"]]), [-0.5, 0.5])
    X[:, COL["is_expiry_day"]] = 0.0
    X[0, COL["is_expiry_day"]] = 1.0
    m = RegimeClassifier.fit(X, trend, vol, horizon_minutes=30, seeds=SEEDS, num_boost_round=40)
    preds = m.predict(X[:50])
    for p in preds:
        assert set(p.trend) == set(TREND_CLASSES) and set(p.vol) == set(VOL_CLASSES)
        assert sum(p.trend.values()) == pytest.approx(1.0, abs=1e-3)
    assert preds[0].expiry_day and preds[0].label.endswith("|EXPIRY_DAY") and not preds[1].expiry_day
    acc = np.mean([TREND_CLASSES.index(p.trend_label) == t for p, t in zip(m.predict(X), trend, strict=True)])
    assert acc > 0.8
    back = RegimeClassifier.load(tmp_path, m.save(tmp_path))
    np.testing.assert_allclose(back.predict_proba(X)[0], m.predict_proba(X)[0])


def test_rule_based_regime_from_trailing_features() -> None:
    f = dict.fromkeys(FEATURE_NAMES, float("nan"))
    f.update(regime_trend=1.0, regime_vol=-1.0, is_expiry_day=1.0)
    p = RuleBasedRegime.from_features(f)
    assert p.label == "TRENDING_UP|LOW_VOL|EXPIRY_DAY" and p.source == "rules"
    f.update(regime_trend=float("nan"), is_expiry_day=0.0)
    p2 = RuleBasedRegime.from_features(f)
    assert sum(p2.trend.values()) == pytest.approx(1.0, abs=1e-3) and not p2.expiry_day
