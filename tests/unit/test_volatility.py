"""volatility/: estimators, expected move, calibration and surface helpers.

Simulated paths here are test fixtures with a KNOWN volatility (to check the
estimators recover it); they are never research evidence.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from volatility import CALENDAR_DAYS, TRADING_DAYS
from volatility.calibration import (
    band_hit_rate,
    block_bootstrap_ratio_ci,
    exceedance_clustering,
    excess_kurtosis,
    mincer_zarnowitz,
    mse,
    normal_coverage,
    qlike,
    variance_ratio,
    wilson_interval,
)
from volatility.expected_move import (
    EXPECTED_RANGE_FACTOR,
    expected_move,
    horizon_sigma,
    horizon_years,
)
from volatility.realised import (
    annualise,
    close_to_close_vol,
    ewma_var,
    garman_klass_var,
    intraday_realised_var,
    log_returns,
    parkinson_var,
    rolling_mean,
    yang_zhang_var,
)
from volatility.surface import IvPoint, atm_iv, iv_rank, percentile_rank, put_call_skew, term_structure


def gbm_days(n_days: int, sigma_day: float, *, steps: int = 375, overnight_share: float = 0.0,
             seed: int = 1) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """OHLC of driftless log-Brownian days with per-day log sd ``sigma_day``;
    ``overnight_share`` of the variance is a gap before the open."""
    rng = np.random.default_rng(seed)
    s_on = sigma_day * math.sqrt(overnight_share)
    s_in = sigma_day * math.sqrt(1 - overnight_share) / math.sqrt(steps)
    o, h, lo, c = (np.empty(n_days) for _ in range(4))
    level = math.log(25000.0)
    paths = []
    for d in range(n_days):
        level += rng.normal(0, s_on) if s_on else 0.0
        path = level + np.concatenate(([0.0], np.cumsum(rng.normal(0, s_in, steps))))
        o[d], h[d], lo[d], c[d] = path[0], path.max(), path.min(), path[-1]
        paths.append(np.exp(path))
        level = path[-1]
    return np.exp(o), np.exp(h), np.exp(lo), np.exp(c), np.array(paths)


# --------------------------------------------------------------------------- #
# realised
# --------------------------------------------------------------------------- #
def test_log_returns_and_rolling_mean() -> None:
    r = log_returns([100.0, 110.0, 99.0])
    assert math.isnan(r[0]) and r[1] == pytest.approx(math.log(1.1)) and r[2] == pytest.approx(math.log(0.9))
    m = rolling_mean([1.0, 2.0, 3.0, np.nan, 5.0, 6.0], 2)
    assert np.isnan(m[0]) and m[1] == 1.5 and m[2] == 2.5 and np.isnan(m[3]) and np.isnan(m[4]) and m[5] == 5.5
    assert np.isnan(rolling_mean([1.0], 3)).all()
    with pytest.raises(ValueError):
        rolling_mean([1.0], 0)


def test_close_to_close_vol_of_alternating_one_percent_moves() -> None:
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * math.exp(0.01 if i % 2 else -0.01))
    vol = close_to_close_vol(closes, 20)
    assert vol[-1] == pytest.approx(0.01 * math.sqrt(TRADING_DAYS))
    assert np.isnan(vol[19]) and not np.isnan(vol[20])


def test_parkinson_garman_klass_and_yang_zhang_recover_a_known_volatility() -> None:
    sd = 0.01
    o, h, lo, c, _ = gbm_days(1500, sd, steps=4000, overnight_share=0.25, seed=3)
    intraday_var = sd**2 * 0.75
    # a fine path: the formulas are right (the discrete-sampling bias is small here)
    assert np.mean(parkinson_var(h, lo)) == pytest.approx(intraday_var, rel=0.06)
    assert np.mean(garman_klass_var(o, h, lo, c)) == pytest.approx(intraday_var, rel=0.06)
    yz = yang_zhang_var(o, h, lo, c, 60)
    assert np.nanmean(yz) == pytest.approx(sd**2, rel=0.08)  # includes the overnight gap
    r = log_returns(c)
    assert np.nanmean(r**2) == pytest.approx(sd**2, rel=0.08)
    assert annualise(np.array([sd**2]))[0] == pytest.approx(sd * math.sqrt(TRADING_DAYS))
    with pytest.raises(ValueError):
        yang_zhang_var(o, h, lo, c, 1)


def test_range_estimators_are_biased_low_on_coarse_paths() -> None:
    """Documented property: highs/lows seen on a coarse path understate the
    true range, so Parkinson / Garman-Klass read low (here 375 steps a day)."""
    o, h, lo, c, _ = gbm_days(2000, 0.01, steps=375, seed=9)
    gk = np.mean(garman_klass_var(o, h, lo, c)) / 0.01**2
    assert 0.82 < gk < 0.97


def test_garman_klass_is_never_negative() -> None:
    assert garman_klass_var([100.0], [100.0], [100.0], [100.0])[0] == 0.0


def test_intraday_realised_var_sampling() -> None:
    closes = [101.0, 102.0, 101.0, 100.0, 101.0]
    rv1 = intraday_realised_var(100.0, closes, step=1)
    expected = sum(math.log(b / a) ** 2 for a, b in zip([100.0] + closes[:-1], closes, strict=True))
    assert rv1 == pytest.approx(expected)
    rv2 = intraday_realised_var(100.0, closes, step=2)  # 100 -> 102 -> 100 -> 101 (last kept)
    assert rv2 == pytest.approx(math.log(1.02) ** 2 + math.log(100 / 102) ** 2 + math.log(1.01) ** 2)
    assert math.isnan(intraday_realised_var(100.0, []))
    _, _, _, _, paths = gbm_days(400, 0.01, seed=5)
    rv5 = np.array([intraday_realised_var(p[0], p[1:], step=5) for p in paths])
    assert rv5.mean() == pytest.approx(0.01**2, rel=0.08)


def test_ewma_is_point_in_time() -> None:
    rng = np.random.default_rng(2)
    r = rng.normal(0, 0.01, 200)
    base = ewma_var(r)
    bumped = r.copy()
    bumped[150] = 0.2
    after = ewma_var(bumped)
    assert after[150] == base[150]  # the forecast for day 150 cannot see day 150
    assert after[151] > base[151]
    assert np.isnan(base[:20]).all() and not np.isnan(base[20])
    with pytest.raises(ValueError):
        ewma_var(r, 1.0)


# --------------------------------------------------------------------------- #
# expected move
# --------------------------------------------------------------------------- #
def test_expected_move_conventions() -> None:
    assert horizon_years(calendar_days=365) == 1.0 and horizon_years(sessions=TRADING_DAYS) == 1.0
    with pytest.raises(ValueError):
        horizon_years()
    with pytest.raises(ValueError):
        horizon_years(calendar_days=1, sessions=1)
    em = expected_move(25000.0, 0.146, horizon_years(calendar_days=1))
    assert em.sigma == pytest.approx(0.146 / math.sqrt(CALENDAR_DAYS))
    assert em.move == pytest.approx(25000 * 0.146 / math.sqrt(365))  # about 191 points
    lo, hi = em.band(2)
    assert lo == pytest.approx(25000 * math.exp(-2 * em.sigma)) and hi > 25000 + 2 * em.move * 0.99
    friday_to_monday = horizon_sigma(0.146, horizon_years(calendar_days=3))
    assert friday_to_monday == pytest.approx(em.sigma * math.sqrt(3))
    assert pytest.approx(1.5958, abs=1e-4) == EXPECTED_RANGE_FACTOR
    with pytest.raises(ValueError):
        expected_move(0.0, 0.1, 0.1)


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #
def test_wilson_and_band_hit_rates_on_normal_data() -> None:
    lo, hi = wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    z = np.random.default_rng(4).normal(size=20000)
    one, two = band_hit_rate(z, 1), band_hit_rate(np.append(z, np.nan), 2)
    assert one.low <= normal_coverage(1) <= one.high and two.n == 20000
    assert two.rate == pytest.approx(0.9545, abs=0.006)
    assert one.as_dict()["normal"] == 0.6827


def test_variance_ratio_bootstrap_qlike_and_mz() -> None:
    rng = np.random.default_rng(5)
    h = rng.uniform(0.5, 2.0, 3000) * 1e-4
    proxy = h * rng.chisquare(1, 3000)  # noisy, unbiased proxy of h
    assert variance_ratio(proxy, h) == pytest.approx(1.0, abs=0.08)
    assert variance_ratio(proxy, h * 1.25) == pytest.approx(0.8, abs=0.07)  # forecast 25 % too high
    lo, hi = block_bootstrap_ratio_ci(proxy, h)
    assert lo < 1.0 < hi
    assert qlike(proxy, h) < qlike(proxy, h * 1.5) and qlike(proxy, h) < qlike(proxy, h * 0.6)
    assert mse(proxy, h) < mse(proxy, h * 3)
    mz = mincer_zarnowitz(proxy, h)
    assert mz.b == pytest.approx(1.0, abs=0.2) and mz.n == 3000
    assert math.isnan(mincer_zarnowitz([1.0, 2.0], [1.0, 1.0]).b)
    assert all(math.isnan(x) for x in block_bootstrap_ratio_ci([1.0] * 10, [1.0] * 10))  # too short


def test_clustering_and_kurtosis() -> None:
    z = np.random.default_rng(6).normal(size=50000)
    cond, base, n = exceedance_clustering(z, 2)
    assert base == pytest.approx(1 - normal_coverage(2), abs=0.004) and n > 1000
    assert cond == pytest.approx(base, abs=0.02)  # independent days: no clustering
    clustered = np.repeat(z[:25000], 2)  # every value twice -> strong clustering
    assert exceedance_clustering(clustered, 2)[0] > 0.45
    assert excess_kurtosis(z) == pytest.approx(0.0, abs=0.1)
    assert excess_kurtosis(np.random.default_rng(7).standard_t(4, 50000)) > 2.0


# --------------------------------------------------------------------------- #
# surface
# --------------------------------------------------------------------------- #
def test_atm_iv_skew_term_structure_rank_percentile() -> None:
    pts = [IvPoint(25100, "CE", 0.13), IvPoint(25100, "PE", 0.14), IvPoint(25150, "CE", 0.125),
           IvPoint(25000, "PE", 0.15), IvPoint(25200, "CE", None)]
    assert atm_iv(pts, 25120.0) == pytest.approx(0.135)
    assert atm_iv([IvPoint(25100, "CE", None)], 25100.0) is None
    # put target 25020 -> 25000 PE (0.15); call target 25220 -> nearest CE with an IV is 25150 (0.125)
    assert put_call_skew(pts, 25120.0, 100) == pytest.approx(0.15 - 0.125)
    assert put_call_skew([IvPoint(1, "CE", 0.1)], 1.0, 1) is None
    from datetime import date
    assert term_structure({date(2026, 11, 24): 0.14, date(2026, 11, 17): 0.13, date(2026, 12, 1): None}) == \
        [(date(2026, 11, 17), 0.13), (date(2026, 11, 24), 0.14)]
    hist = [10.0, 12.0, 14.0, 16.0, 20.0]
    assert percentile_rank(hist, 15.0) == 0.6 and iv_rank(hist, 15.0) == pytest.approx(0.5)
    assert math.isnan(iv_rank([1.0, 1.0], 1.0)) and math.isnan(percentile_rank([], 1.0))
