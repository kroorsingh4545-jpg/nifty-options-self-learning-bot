"""Forecast-quality analysis and the report, on synthetic data with KNOWN answers
(fixtures only; never research evidence)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.clock import IST
from core.models import Candle
from research.config import load_research_config
from research.forecast.analysis import (
    calibration,
    compare_forecasters,
    full_report,
    overnight_intraday,
    summarize,
    vrp,
    weekend_multiple,
)
from research.forecast.run import run_build
from research.reports.forecast_report import render_markdown
from research.store import ResearchStore

RCFG = load_research_config()
NIFTY, VIX = RCFG.instruments.nifty_index_key, RCFG.instruments.india_vix_key


def joined(n: int = 3000, *, scale: float = 1.0, weekend_var: float = 1.0, seed: int = 1) -> pd.DataFrame:
    """Joined forecast/outcome frame: sigma known, returns = scale * sigma * z."""
    rng = np.random.default_rng(seed)
    days, d = [], date(2012, 1, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    prev = [days[0] - timedelta(days=3)] + days[:-1]
    cal = np.array([(a - b).days for a, b in zip(days, prev, strict=True)])
    sigma = 0.01 * np.exp(rng.normal(0, 0.2, n))
    mult = np.where(cal == 3, math.sqrt(weekend_var), 1.0)
    r = scale * sigma * mult * rng.normal(size=n)
    gap = r * 0.5
    return pd.DataFrame({
        "session_date": days, "prev_date": [p.isoformat() for p in prev], "cal_days": cal,
        "vix_prev": 100 * sigma * math.sqrt(365), "sigma_vix_cal": sigma, "sigma_vix_trd": sigma * 0.9,
        "sigma_rv20": sigma * 1.3, "sigma_ewma": sigma * np.exp(rng.normal(0, 0.5, n)), "sigma_har": sigma,
        "vol_regime": rng.choice(["LOW", "MID", "HIGH"], n), "trend_regime": rng.choice(["UP", "DOWN", "RANGE"], n),
        "vix_chg5": rng.normal(0, 0.1, n), "dte": [i % 5 for i in range(n)], "is_expiry": [int(i % 5 == 0)
                                                                                              for i in range(n)],
        "expiry_shifted": 0, "is_monthly_expiry": 0, "r_cc": r, "r_gap": gap, "gap_reliable": 1, "r_oc": r - gap,
        "ln_hl": np.abs(r) + 0.005, "gk_var": (r - gap) ** 2, "rv5_var": (r - gap) ** 2,
    })


def test_summarize_unbiased_and_too_wide_forecasts() -> None:
    fair = summarize(joined(), "sigma_vix_cal", block=20)
    assert fair["variance_ratio"] == pytest.approx(1.0, abs=0.08)
    assert fair["variance_ratio_ci95"][0] < 1.0 < fair["variance_ratio_ci95"][1]
    assert fair["hit_1sd"]["rate"] == pytest.approx(0.683, abs=0.03)
    wide = summarize(joined(scale=0.8), "sigma_vix_cal", block=20)  # forecast 25 % too wide
    assert wide["variance_ratio"] == pytest.approx(0.64, abs=0.06) and wide["hit_1sd"]["rate"] > 0.75
    assert summarize(joined(20), "sigma_vix_cal", block=20) == {"n": 20}


def test_weekend_multiple_recovers_the_weekend_variance() -> None:
    w = weekend_multiple(joined(20000, weekend_var=2.0))
    assert w["multiple"] == pytest.approx(2.0, rel=0.12) and w["ci95"][0] < w["multiple"] < w["ci95"][1]
    assert weekend_multiple(joined(20)) == {"n_weekend": 4, "n_normal": 16}


def test_calibration_tables_and_surprises() -> None:
    c = calibration(joined(), block=20)
    assert c["overall"]["sigma_vix_cal"]["n"] == 3000
    assert {r["bucket"] for r in c["by_vol_regime"]} == {"LOW", "MID", "HIGH"}
    assert {r["bucket"] for r in c["by_sessions_to_expiry"]} == {"0", "1", "2", "3", "4+"}
    assert len(c["largest_surprises"]) == 12
    zs = [abs(s["z"]) for s in c["largest_surprises"]]
    assert zs == sorted(zs, reverse=True)
    assert c["clustering_2sd"]["unconditional"] == pytest.approx(0.0455, abs=0.012)


def test_overnight_share() -> None:
    o = overnight_intraday(joined())
    assert o["overnight_share"] == pytest.approx(0.5, abs=0.01)  # gap = half the return, oc = the other half


def test_compare_forecasters_ranks_the_true_sigma_first() -> None:
    f = compare_forecasters(joined(4000))
    ranking = f["proxies"]["squared_return"]["ranking_by_qlike"]
    assert ranking[0] in {"sigma_vix_cal", "sigma_har"}  # both carry the true sigma
    assert ranking[-1] in {"sigma_ewma", "sigma_rv20"}
    m = f["proxies"]["squared_return"]["metrics"]["sigma_rv20"]
    assert m["bias_ratio"] == pytest.approx(1 / 1.69, abs=0.06)  # 30 % too wide -> variance 1.69x


def test_vrp_matched_horizon() -> None:
    # sigma is per session but VIX is quoted per calendar day: 30 calendar days hold about 21.4
    # sessions, so realised = 0.75 * sqrt(21.43 / 30) = 63 % of implied
    df = joined(1500, scale=0.75)
    out = vrp(df)
    implied = float(df["vix_prev"].mean())
    assert out["mean_vol_points"] > 0 and out["share_vix_above_realised"] > 0.9
    assert out["mean_vol_points"] == pytest.approx(implied * (1 - 0.75 * math.sqrt(21.43 / 30)), rel=0.2)


# --------------------------------------------------------------------------- #
# full report from a store
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> Iterator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "research.db")
    rng = np.random.default_rng(5)
    candles, level, d = [], 10000.0, date(2015, 1, 1)
    while d <= date(2019, 6, 28):
        if d.weekday() < 5:
            start = datetime.combine(d, time(0), tzinfo=IST)
            o = level * math.exp(rng.normal(0, 0.003))
            c = o * math.exp(rng.normal(0, 0.008))
            h, lo = max(o, c) * 1.004, min(o, c) * 0.996
            vix = 15 * math.exp(rng.normal(0, 0.1))
            for key, ohlc in ((NIFTY, (o, h, lo, c)), (VIX, (vix, vix, vix, vix))):
                candles.append(Candle(instrument_key=key, timeframe_minutes=1440, start=start,
                                      end=start + timedelta(days=1), open=ohlc[0], high=ohlc[1], low=ohlc[2],
                                      close=ohlc[3]))
            level = c
        d += timedelta(days=1)
    s.upsert_daily(candles)
    run_build(s, RCFG, configured_holidays=[], now=datetime(2019, 6, 29, 18, 0, tzinfo=IST))
    yield s
    s.close()


def test_full_report_and_markdown(store: ResearchStore) -> None:
    rep = full_report(store, NIFTY, VIX, block=20)
    json.dumps(rep)  # JSON-serialisable
    assert rep["current"]["next_session"]["date"] == "2019-07-01"
    assert rep["current"]["next_session"]["recorded_live"] is True
    assert "calibrated_move_points_1sd" in rep["current"]["next_session"]
    assert any("chain snapshots" in w for w in rep["current"]["data_quality"])
    md = render_markdown(rep)
    for heading in ("## 1. Overall calibration", "## 2. Weekends", "## 5. Overnight vs intraday",
                    "## 7. Volatility risk premium", "## 9. Current state", "Data-quality warnings"):
        assert heading in md
    store.save_report("forecast_vs_reality", json.dumps(rep))
    assert store.count("reports") == 1
