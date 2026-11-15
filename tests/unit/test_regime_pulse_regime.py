"""D4: Momentum Pulse labels and the session regime (SYNTHETIC inputs)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ml.labeling import TREND_CLASSES, VOL_CLASSES
from ml.regime_classifier import RegimePrediction
from research.regime.pulse import (
    DEVELOPING,
    RANGE_CHOP,
    STRONG_TREND,
    UNCERTAIN,
    WEAK,
    fit,
    pulse,
)
from research.regime.regime import CONTRACTION, EXPANSION, STABLE, classify, named


def training(n: int = 1000, seed: int = 4) -> list[dict[str, float]]:
    rng = np.random.default_rng(seed)
    return [{"er": float(rng.uniform(0, 1)), "adx": float(rng.uniform(5, 60)), "abs_disp": float(rng.uniform(0, 5)),
             "atrx": float(rng.uniform(0.5, 1.5)), "disp": 0.0} for _ in range(n)]


CUT = fit(training())


def at(p_er: float, p_adx: float, p_disp: float, p_atrx: float, sign: float = 1.0) -> dict[str, float]:
    """Features sitting at the given training percentiles."""
    return {"er": p_er, "adx": 5 + 55 * p_adx, "abs_disp": 5 * p_disp, "atrx": 0.5 + p_atrx, "disp": sign * 5 * p_disp}


def test_cutoffs_are_percentiles_of_the_training_data() -> None:
    assert CUT.n_train == 1000
    assert CUT.percentile("er", 0.5) == pytest.approx(0.5, abs=0.05)
    assert CUT.percentile("adx", 1e9) == 1.0 and CUT.percentile("adx", -1.0) == 0.0
    with pytest.raises(ValueError):
        fit(training(50))
    assert fit([*training(), {"er": math.nan, "adx": 1.0, "abs_disp": 1.0, "atrx": 1.0}]).n_train == 1000


@pytest.mark.parametrize(("feats", "label"), [
    (at(0.9, 0.9, 0.9, 0.5), STRONG_TREND),
    (at(0.1, 0.1, 0.2, 0.5), RANGE_CHOP),
    (at(0.6, 0.5, 0.6, 0.8), DEVELOPING),
    (at(0.5, 0.5, 0.4, 0.5), WEAK),
    (at(0.9, 0.2, 0.9, 0.9), DEVELOPING),  # efficient and expanding, but ADX not yet strong
])
def test_labels_need_several_components_to_agree(feats: dict[str, float], label: str) -> None:
    p = pulse(feats, CUT)
    assert p.label == label and 0.0 <= p.confidence <= 1.0 and len(p.reasons) == 4


def test_one_extreme_component_alone_is_never_a_strong_trend() -> None:
    for single in (at(0.99, 0.5, 0.5, 0.5), at(0.5, 0.99, 0.5, 0.5), at(0.5, 0.5, 0.99, 0.5)):
        assert pulse(single, CUT).label != STRONG_TREND


def test_direction_confidence_and_uncertain() -> None:
    up, down = pulse(at(0.95, 0.95, 0.95, 0.5), CUT), pulse(at(0.95, 0.95, 0.95, 0.5, -1.0), CUT)
    assert (up.direction, down.direction) == (1, -1)
    assert pulse(at(0.99, 0.99, 0.99, 0.5), CUT).confidence > pulse(at(0.75, 0.75, 0.75, 0.5), CUT).confidence
    u = pulse({"er": math.nan, "adx": 20.0, "abs_disp": 1.0, "atrx": 1.0}, CUT)
    assert u.label == UNCERTAIN and "efficiency ratio" in u.reasons[0]


# Regime -------------------------------------------------------------------------


def history(n: int = 400, seed: int = 9, vix: float = 14.0) -> tuple[list[float], list[float]]:
    rng = np.random.default_rng(seed)
    nifty = list(20000 * np.exp(np.cumsum(rng.normal(0, 0.009, n))))
    vixs = list(vix * np.exp(np.cumsum(rng.normal(0, 0.04, n)) * 0.3))
    return nifty, vixs


def test_the_regime_is_an_ml_regime_prediction() -> None:
    nifty, vix = history()
    p = classify(vix_now=float(np.median(vix[-252:])), vix_closes_before=vix, nifty_closes_before=nifty,
                 open_today=nifty[-1], expiry_day=True)
    assert isinstance(p, RegimePrediction) and set(p.trend) == set(TREND_CLASSES) and set(p.vol) == set(VOL_CLASSES)
    assert p.vol_label == "NORMAL_VOL" and p.expiry_day and p.source == "rules-d4"
    assert p.extra["reasons"] and 0.0 <= p.extra["confidence"] <= 1.0


@pytest.mark.parametrize(("q", "label"), [(0.05, "LOW_VOL"), (0.5, "NORMAL_VOL"), (0.95, "HIGH_VOL")])
def test_volatility_level_is_a_tercile_of_the_trailing_year(q: float, label: str) -> None:
    nifty, vix = history()
    now = float(np.quantile(vix[-252:], q))
    assert classify(vix_now=now, vix_closes_before=vix, nifty_closes_before=nifty, open_today=None,
                    expiry_day=False).vol_label == label


def test_vix_change_expansion_contraction_and_unstable() -> None:
    nifty, vix = history()
    up = classify(vix_now=vix[-5] * 1.4, vix_closes_before=vix, nifty_closes_before=nifty, open_today=None,
                  expiry_day=False)
    down = classify(vix_now=vix[-5] * 0.75, vix_closes_before=vix, nifty_closes_before=nifty, open_today=None,
                    expiry_day=False)
    flat = classify(vix_now=vix[-5], vix_closes_before=vix, nifty_closes_before=nifty, open_today=None,
                    expiry_day=False)
    assert up.extra["vol_change"] == EXPANSION and down.extra["vol_change"] == CONTRACTION
    assert flat.extra["vol_change"] in (STABLE, EXPANSION, CONTRACTION)
    assert named(down) == "VOL_CONTRACTION"
    gap = classify(vix_now=vix[-1], vix_closes_before=vix, nifty_closes_before=nifty, open_today=nifty[-1] * 1.08,
                   expiry_day=False)
    assert gap.extra["unstable"] and named(gap) == "UNSTABLE"


def test_daily_trend_from_the_last_sessions() -> None:
    nifty, vix = history()
    rising = nifty[:-20] + [nifty[-21] * math.exp(0.006 * k) for k in range(1, 21)]
    p = classify(vix_now=vix[-1], vix_closes_before=vix, nifty_closes_before=rising, open_today=None, expiry_day=False)
    assert p.trend_label == "TRENDING_UP" and p.extra["trend_z"] > 1


def test_too_little_history_gives_no_label_on_that_axis() -> None:
    p = classify(vix_now=14.0, vix_closes_before=[14.0] * 10, nifty_closes_before=[20000.0] * 10, open_today=None,
                 expiry_day=False)
    assert len(set(p.vol.values())) == 1 and len(set(p.trend.values())) == 1 and named(p) == "UNKNOWN"
