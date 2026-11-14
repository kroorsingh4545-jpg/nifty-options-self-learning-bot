"""Forecast-vs-reality builder: point-in-time forecasts, outcomes, HAR, expiry
context, live-forecast immutability (synthetic fixture panels, never evidence)."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.clock import IST
from core.models import Candle
from research.config import load_research_config
from research.forecast.build import (
    LookaheadError,
    build_forecasts,
    build_outcomes,
    expiry_contexts,
    har_forecast,
    next_session_after,
)
from research.forecast.panel import load_panel
from research.forecast.run import run_build
from research.sessions import SessionCalendar
from research.store import ChainRow, ResearchStore

RCFG = load_research_config()
FC = RCFG.forecast
NIFTY, VIX = RCFG.instruments.nifty_index_key, RCFG.instruments.india_vix_key


def weekdays(a: date, b: date, closed: tuple[date, ...] = ()) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_panel(days: list[date], *, seed: int = 1, sd: float = 0.01,
                    special: tuple[date, ...] = ()) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 10000 * np.exp(np.cumsum(rng.normal(0, sd, len(days))))
    o = np.concatenate(([10000.0], c[:-1])) * np.exp(rng.normal(0, sd / 3, len(days)))
    hi = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, sd / 2, len(days))))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, sd / 2, len(days))))
    vix = 100 * sd * math.sqrt(252) * np.exp(rng.normal(0, 0.1, len(days)))
    return pd.DataFrame({"session_date": days, "o": o, "h": hi, "l": lo, "c": c, "vix": vix,
                         "rv5": np.nan, "n_minutes": np.nan, "special": [d in special for d in days]})


DAYS = weekdays(date(2017, 1, 2), date(2020, 12, 31))
CAL = SessionCalendar.from_sessions(DAYS)
NOW = datetime(2021, 1, 1, 18, 0, tzinfo=IST)


def _by_date(rows: list[dict]) -> dict[str, dict]:
    return {r["session_date"]: r for r in rows}


# --------------------------------------------------------------------------- #
# point in time
# --------------------------------------------------------------------------- #
SIGMAS = ("sigma_vix_cal", "sigma_vix_trd", "sigma_rv20", "sigma_ewma", "sigma_har", "vix_pctl", "vix_chg5", "trend_z")


def test_a_session_outcome_never_changes_its_own_forecast() -> None:
    panel = synthetic_panel(DAYS)
    base = _by_date(build_forecasts(panel, FC, CAL, RCFG.weekly_expiry_rules, now=NOW, next_session=None))
    i = 900
    shocked = panel.copy()
    for col, f in (("c", 1.2), ("h", 1.3), ("o", 1.1), ("l", 0.9), ("vix", 3.0)):
        shocked.loc[i, col] = shocked.loc[i, col] * f
    after = _by_date(build_forecasts(shocked, FC, CAL, RCFG.weekly_expiry_rules, now=NOW, next_session=None))
    t, t1 = DAYS[i].isoformat(), DAYS[i + 1].isoformat()
    for k in SIGMAS:
        assert base[t][k] == after[t][k], k  # the shocked day's own forecast is unchanged
    assert after[t1]["sigma_vix_cal"] != base[t1]["sigma_vix_cal"]  # the next day sees it
    assert after[t1]["sigma_rv20"] != base[t1]["sigma_rv20"]
    assert after[t1]["sigma_ewma"] != base[t1]["sigma_ewma"]


def test_har_uses_only_the_past() -> None:
    rng = np.random.default_rng(3)
    v = np.exp(rng.normal(-9, 0.5, 900))
    base = har_forecast(v, min_train=500, refit_every=21)
    bumped = v.copy()
    bumped[700] *= 50
    after = har_forecast(bumped, min_train=500, refit_every=21)
    assert np.array_equal(base[:701], after[:701], equal_nan=True)
    assert after[701] != base[701]
    assert np.isnan(base[:522]).all() and np.isfinite(base[522])


def test_har_on_constant_variance_predicts_it() -> None:
    v = np.full(700, 1e-4) * np.exp(np.random.default_rng(4).normal(0, 0.01, 700))
    f = har_forecast(v, min_train=500, refit_every=21)
    assert np.nanmean(f[600:]) == pytest.approx(1e-4, rel=0.02)


def test_next_session_must_be_after_the_data() -> None:
    panel = synthetic_panel(DAYS)
    with pytest.raises(LookaheadError):
        build_forecasts(panel, FC, CAL, RCFG.weekly_expiry_rules, now=NOW, next_session=DAYS[-1])


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
def test_vix_sigma_conventions_weekend_and_values() -> None:
    panel = synthetic_panel(DAYS)
    rows = _by_date(build_forecasts(panel, FC, CAL, RCFG.weekly_expiry_rules, now=NOW, next_session=None))
    monday = next(d for d in DAYS[300:] if d.weekday() == 0)  # enough history for a regime
    r = rows[monday.isoformat()]
    friday_vix = float(panel.loc[panel.session_date == monday - timedelta(days=3), "vix"].iloc[0])
    assert r["cal_days"] == 3 and r["vix_prev"] == pytest.approx(friday_vix)
    assert r["sigma_vix_cal"] == pytest.approx(friday_vix / 100 * math.sqrt(3 / 365))
    assert r["sigma_vix_trd"] == pytest.approx(friday_vix / 100 / math.sqrt(252))
    assert r["vol_regime"] in {"LOW", "MID", "HIGH"} and r["trend_regime"] in {"UP", "DOWN", "RANGE"}
    assert r["recorded_live"] == 0 and r["method_version"] == FC.method_version


def test_special_sessions_are_used_as_history_but_never_forecast() -> None:
    sunday = date(2019, 10, 27)  # a Muhurat-style Sunday session
    days = sorted(DAYS + [sunday])
    panel = synthetic_panel(days, special=(sunday,))
    cal = SessionCalendar.from_sessions(days, configured_holidays=[])
    rows = _by_date(build_forecasts(panel, FC, cal, RCFG.weekly_expiry_rules, now=NOW, next_session=None))
    assert sunday.isoformat() not in rows
    monday = rows["2019-10-28"]
    assert monday["prev_date"] == "2019-10-27" and monday["cal_days"] == 1
    assert "special session" in monday["data_issue"]
    outcomes = _by_date(build_outcomes(panel, FC, now=NOW))
    assert sunday.isoformat() not in outcomes
    sunday_close = float(panel.loc[panel.session_date == sunday, "c"].iloc[0])
    assert outcomes["2019-10-28"]["prev_close"] == pytest.approx(sunday_close)


def test_missing_vix_is_left_missing_and_flagged() -> None:
    panel = synthetic_panel(DAYS)
    panel.loc[500, "vix"] = np.nan
    rows = _by_date(build_forecasts(panel, FC, CAL, RCFG.weekly_expiry_rules, now=NOW, next_session=None))
    r = rows[DAYS[501].isoformat()]
    assert r["sigma_vix_cal"] is None and r["vix_prev"] is None and "no India VIX close" in r["data_issue"]
    assert r["sigma_rv20"] is not None  # other forecasters still work


def test_outcome_values() -> None:
    panel = synthetic_panel(DAYS)
    rows = _by_date(build_outcomes(panel, FC, now=NOW))
    p, c = panel.iloc[100], panel.iloc[101]
    r = rows[c.session_date.isoformat()]
    assert r["r_cc"] == pytest.approx(math.log(c.c / p.c)) and r["r_gap"] == pytest.approx(math.log(c.o / p.c))
    assert r["r_oc"] == pytest.approx(math.log(c.c / c.o)) and r["ln_hl"] == pytest.approx(math.log(c.h / c.l))
    assert r["gap_reliable"] == 1 and r["rv5_var"] is None
    early = synthetic_panel(weekdays(date(2010, 12, 20), date(2011, 1, 10)))
    rel = {x["session_date"]: x["gap_reliable"] for x in build_outcomes(early, FC, now=NOW)}
    assert rel["2010-12-31"] == 0 and rel["2011-01-03"] == 1


# --------------------------------------------------------------------------- #
# expiry context
# --------------------------------------------------------------------------- #
def test_expiry_context_with_holiday_shift_and_monthly() -> None:
    days = weekdays(date(2026, 10, 26), date(2026, 11, 30), closed=(date(2026, 11, 10), date(2026, 11, 24)))
    cal = SessionCalendar.from_sessions(days)
    ctx = expiry_contexts([date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 11),
                           date(2026, 11, 23), date(2026, 11, 25), date(2019, 1, 7)], RCFG.weekly_expiry_rules, cal)
    assert (ctx[date(2026, 11, 5)].dte, ctx[date(2026, 11, 6)].dte) == (2, 1)
    assert ctx[date(2026, 11, 9)].is_expiry and ctx[date(2026, 11, 9)].shifted  # Tue 10 Nov holiday -> Mon 9 Nov
    assert ctx[date(2026, 11, 11)].dte == 4 and not ctx[date(2026, 11, 11)].shifted  # -> 17 Nov
    assert ctx[date(2026, 11, 23)].is_expiry and ctx[date(2026, 11, 23)].monthly  # last expiry of November
    assert not ctx[date(2026, 11, 9)].monthly
    assert ctx[date(2019, 1, 7)].dte is None  # before NIFTY weekly options


def test_next_session_after_skips_weekends_and_configured_holidays() -> None:
    cal = SessionCalendar.from_sessions(weekdays(date(2026, 11, 2), date(2026, 11, 20)),
                                        configured_holidays=[date(2026, 11, 24)])
    assert next_session_after(cal, date(2026, 11, 20)) == date(2026, 11, 23)
    assert next_session_after(cal, date(2026, 11, 23)) == date(2026, 11, 25)


# --------------------------------------------------------------------------- #
# store + run
# --------------------------------------------------------------------------- #
def _fill_store(store: ResearchStore, days: list[date]) -> pd.DataFrame:
    panel = synthetic_panel(days, seed=11)
    candles = []
    for r in panel.itertuples():
        start = datetime.combine(r.session_date, time(0), tzinfo=IST)
        for key, (o, h, lo, c) in ((NIFTY, (r.o, r.h, r.l, r.c)), (VIX, (r.vix, r.vix, r.vix, r.vix))):
            candles.append(Candle(instrument_key=key, timeframe_minutes=1440, start=start,
                                  end=start + timedelta(days=1), open=o, high=h, low=lo, close=c))
    store.upsert_daily(candles, at=NOW)
    return panel


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "research.db")
    yield s
    s.close()


def test_run_build_records_a_live_forecast_that_can_never_change(store: ResearchStore) -> None:
    days = weekdays(date(2023, 1, 2), date(2026, 11, 13))
    _fill_store(store, days)
    friday_evening = datetime(2026, 11, 14, 18, 0, tzinfo=IST)
    s = run_build(store, RCFG, configured_holidays=[], now=friday_evening)
    assert s.next_session == date(2026, 11, 16) and s.next_recorded_live and not s.warnings
    live = dict(store.con.execute("SELECT * FROM vol_forecasts WHERE session_date='2026-11-16'").fetchone())
    assert live["recorded_live"] == 1
    before = store.con.execute("SELECT vix_prev FROM vol_forecasts WHERE session_date='2026-11-13'").fetchone()[0]
    # a later rebuild (even with changed inputs) keeps the live row exactly as recorded
    store.con.execute("UPDATE daily_candles SET open = open * 1.5, high = high * 1.5, low = low * 1.5,"
                      " close = close * 1.5 WHERE instrument_key = ?", (VIX,))
    store.con.commit()
    run_build(store, RCFG, configured_holidays=[], now=friday_evening + timedelta(hours=2))
    again = dict(store.con.execute("SELECT * FROM vol_forecasts WHERE session_date='2026-11-16'").fetchone())
    assert again == live
    with pytest.raises(sqlite3.IntegrityError, match="never be changed"):
        store.con.execute("UPDATE vol_forecasts SET sigma_vix_cal = 0 WHERE session_date='2026-11-16'")
    with pytest.raises(sqlite3.IntegrityError, match="never be deleted"):
        store.con.execute("DELETE FROM vol_forecasts WHERE session_date='2026-11-16'")
    # reconstructed rows are rebuilt from the changed inputs
    rebuilt = store.con.execute("SELECT vix_prev FROM vol_forecasts WHERE session_date='2026-11-13'").fetchone()[0]
    assert rebuilt == pytest.approx(before * 1.5)
    assert store.count("vol_outcomes") == len(days) - 1


def test_run_build_after_the_open_is_not_live_and_stale_history_makes_no_forecast(store: ResearchStore) -> None:
    _fill_store(store, weekdays(date(2023, 1, 2), date(2026, 11, 13)))
    s = run_build(store, RCFG, configured_holidays=[], now=datetime(2026, 11, 16, 9, 20, tzinfo=IST))
    assert s.next_session == date(2026, 11, 16) and not s.next_recorded_live
    stale = run_build(store, RCFG, configured_holidays=[], now=datetime(2026, 11, 18, 18, 0, tzinfo=IST))
    assert stale.next_session is None and "refresh" in stale.warnings[0]


def test_run_build_needs_enough_history(store: ResearchStore) -> None:
    _fill_store(store, weekdays(date(2026, 10, 1), date(2026, 11, 13)))
    s = run_build(store, RCFG, configured_holidays=[], now=NOW)
    assert s.forecasts == 0 and "not enough" in s.warnings[0]


def test_atm_iv_forecast_from_a_snapshot(store: ResearchStore) -> None:
    _fill_store(store, weekdays(date(2023, 1, 2), date(2026, 11, 13)))
    store.record_observation("chain_iv_unit", "percent", "test", at=NOW)
    rows = [ChainRow(25100.0, "CE", "k1", iv_raw=13.0), ChainRow(25100.0, "PE", "k2", iv_raw=14.0),
            ChainRow(25150.0, "CE", "k3", iv_raw=12.0)]
    store.save_chain_snapshot(taken_at=datetime(2026, 11, 13, 15, 35, tzinfo=IST), underlying_key=NIFTY,
                              expiry=date(2026, 11, 17), spot=25110.0, rows=rows)
    run_build(store, RCFG, configured_holidays=[], now=datetime(2026, 11, 14, 18, 0, tzinfo=IST))
    sig = store.con.execute("SELECT sigma_atm_iv FROM vol_forecasts WHERE session_date='2026-11-16'").fetchone()[0]
    assert sig == pytest.approx(0.135 * math.sqrt(3 / 365))
    none = store.con.execute("SELECT sigma_atm_iv FROM vol_forecasts WHERE session_date='2026-11-13'").fetchone()[0]
    assert none is None  # no snapshot for that session


def test_load_panel_joins_vix_and_intraday_rv(store: ResearchStore) -> None:
    days = weekdays(date(2026, 11, 2), date(2026, 11, 6))
    _fill_store(store, days)
    t0 = datetime(2026, 11, 3, 9, 15, tzinfo=IST)
    mins = [Candle(instrument_key=NIFTY, timeframe_minutes=1, start=t0 + timedelta(minutes=k),
                   end=t0 + timedelta(minutes=k + 1), open=100.0, high=101.0, low=99.0, close=100.0 + (k % 2))
            for k in range(375)]
    store.upsert_minutes(mins, at=NOW)
    panel = load_panel(store, NIFTY, VIX, special=[], step=5, min_minutes=360)
    row = panel[panel.session_date == date(2026, 11, 3)].iloc[0]
    assert row.n_minutes == 375 and row.rv5 > 0 and np.isfinite(row.vix)
    assert np.isnan(panel[panel.session_date == date(2026, 11, 4)].iloc[0].rv5)
