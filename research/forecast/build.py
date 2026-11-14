"""Build forecast rows (before each session) and outcome rows (after it).

Point-in-time rule: the forecast for session ``t`` is computed ONLY from rows
dated before ``t`` (the previous session's VIX close and NIFTY close, and
returns up to the previous session). ``LookaheadError`` is raised if a
forecaster is ever handed a row from ``t`` or later. HAR coefficients are
refitted every ``har_refit_every`` sessions on (features, next-day target)
pairs whose target is strictly before the refit session.

Forecasts (all per-session log-return standard deviations):

* ``sigma_vix_cal``: India VIX on calendar time, ``VIX/100 * sqrt(cal_days/365)``
* ``sigma_vix_trd``: India VIX on trading time, ``VIX/100 * sqrt(1/252)``
* ``sigma_rv20``: trailing close-to-close over ``rv_window`` sessions
* ``sigma_ewma``: RiskMetrics EWMA
* ``sigma_har``: HAR-log on the variance proxy ``gap**2 + Garman-Klass``
* ``sigma_atm_iv``: ATM IV of the nearest expiry from an option-chain snapshot
  taken between the previous close and the open (only once snapshots exist;
  assumes a flat term structure to the expiry)
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from core.clock import IST
from research.config import ExpiryRule, ForecastSection
from research.sessions import SessionCalendar, weekly_expiries
from research.store import ResearchStore
from volatility import CALENDAR_DAYS, TRADING_DAYS
from volatility.realised import ewma_var, garman_klass_var, log_returns, parkinson_var, rolling_mean
from volatility.surface import IvPoint, atm_iv

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
SNAPSHOT_FROM = time(15, 0)  # an evening / pre-open snapshot counts for the next session
VAR_FLOOR = 1e-8


class LookaheadError(ValueError):
    pass


def live_deadline(session: date, margin_minutes: int) -> datetime:
    """Latest time a forecast for ``session`` may be made and still count as
    recorded live: ``margin_minutes`` before the 09:15 open (PC clock drift)."""
    return datetime.combine(session, SESSION_OPEN, tzinfo=IST) - timedelta(minutes=margin_minutes)


# --------------------------------------------------------------------------- #
# HAR-log
# --------------------------------------------------------------------------- #
def har_features(v: np.ndarray) -> np.ndarray:
    """Columns [1, ln v_s, ln mean(v_{s-4..s}), ln mean(v_{s-21..s})] per index s."""
    v = np.maximum(np.asarray(v, dtype=np.float64), VAR_FLOOR)
    return np.column_stack([np.ones(len(v)), np.log(v), np.log(rolling_mean(v, 5)), np.log(rolling_mean(v, 22))])


def har_forecast(v: np.ndarray, *, min_train: int, refit_every: int) -> np.ndarray:
    """Variance forecast for index t (from features at t-1), NaN until the
    first fit. ``v`` has one extra trailing slot allowed to be NaN (the next,
    not yet observed session)."""
    x = har_features(v)
    y = np.log(np.maximum(np.asarray(v, dtype=np.float64), VAR_FLOOR))
    n = len(v)
    out = np.full(n, np.nan)
    coef: np.ndarray | None = None
    resid_var = 0.0
    first = min_train + 22
    for t in range(first, n):
        if coef is None or (t - first) % refit_every == 0:
            # pairs (features at s, target at s+1) with s+1 < t: only the past
            xs, ys = x[: t - 1], y[1:t]
            ok = np.isfinite(xs).all(axis=1) & np.isfinite(ys) & np.isfinite(v[1:t])
            if ok.sum() >= min_train:
                coef, *_ = np.linalg.lstsq(xs[ok], ys[ok], rcond=None)
                resid_var = float(np.var(ys[ok] - xs[ok] @ coef))
        if coef is not None and np.isfinite(x[t - 1]).all():
            out[t] = math.exp(float(x[t - 1] @ coef) + 0.5 * resid_var)
    return out


# --------------------------------------------------------------------------- #
# expiry context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpiryContext:
    dte: int | None
    is_expiry: bool | None
    shifted: bool | None
    monthly: bool | None


def expiry_contexts(targets: Sequence[date], rules: Sequence[ExpiryRule],
                    calendar: SessionCalendar) -> dict[date, ExpiryContext]:
    if not targets:
        return {}
    first_rule = rules[0].effective_from
    expiries = weekly_expiries(first_rule, max(targets) + timedelta(days=45), rules, calendar)
    by_month: dict[tuple[int, int], date] = {}
    for e in expiries:
        key = (e.expiry.year, e.expiry.month)
        by_month[key] = max(by_month.get(key, e.expiry), e.expiry)
    out: dict[date, ExpiryContext] = {}
    j = 0
    for t in sorted(targets):
        if t < first_rule:
            out[t] = ExpiryContext(None, None, None, None)
            continue
        while j < len(expiries) and expiries[j].expiry < t:
            j += 1
        if j >= len(expiries):
            out[t] = ExpiryContext(None, None, None, None)
            continue
        e = expiries[j]
        dte, d = 0, t
        while d < e.expiry:
            d += timedelta(days=1)
            if calendar.is_trading_day(d):
                dte += 1
        out[t] = ExpiryContext(dte, dte == 0, e.shifted, by_month[(e.expiry.year, e.expiry.month)] == e.expiry)
    return out


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _f(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def _pctl(vix: np.ndarray, i: int, lookback: int) -> float:
    """Share of the ``lookback`` VIX closes before index i that are below vix[i]."""
    window = vix[max(0, i - lookback):i]
    window = window[np.isfinite(window)]
    if len(window) < lookback // 2 or not np.isfinite(vix[i]):
        return math.nan
    return float(np.mean(window < vix[i]))


def _atm_sigma(store: ResearchStore, underlying: str, prev: date, target: date, cal_days: int) -> float | None:
    unit = store.observation("chain_iv_unit")
    if unit not in ("percent", "decimal"):
        return None
    lo = datetime.combine(prev, SNAPSHOT_FROM, tzinfo=IST).isoformat()
    hi = datetime.combine(target, SESSION_OPEN, tzinfo=IST).isoformat()
    snap = store.con.execute(
        "SELECT snapshot_id, spot FROM chain_snapshots WHERE underlying_key = ? AND taken_at >= ? AND taken_at < ?"
        " AND expiry >= ? ORDER BY expiry, taken_at DESC LIMIT 1", (underlying, lo, hi, target.isoformat())).fetchone()
    if snap is None or not snap["spot"]:
        return None
    scale = 0.01 if unit == "percent" else 1.0
    pts = [IvPoint(r["strike"], r["option_type"], None if r["iv_raw"] is None else r["iv_raw"] * scale)
           for r in store.con.execute("SELECT strike, option_type, iv_raw FROM chain_snapshot_rows"
                                      " WHERE snapshot_id = ?", (snap["snapshot_id"],))]
    iv = atm_iv(pts, float(snap["spot"]))
    return None if iv is None else iv * math.sqrt(cal_days / CALENDAR_DAYS)


def build_forecasts(panel: pd.DataFrame, cfg: ForecastSection, calendar: SessionCalendar,
                    rules: Sequence[ExpiryRule], *, now: datetime, next_session: date | None,
                    store: ResearchStore | None = None, underlying: str = "NSE_INDEX|Nifty 50") -> list[dict[str, Any]]:
    """One forecast per regular session in ``panel`` (from the second row on)
    plus ``next_session`` (not yet observed). Uses only earlier rows."""
    dates: list[date] = list(panel["session_date"])
    if next_session is not None and dates and next_session <= dates[-1]:
        raise LookaheadError("next_session must be after the last observed session")
    n_obs = len(dates)
    all_dates = dates + ([next_session] if next_session is not None else [])
    pad = len(all_dates) - n_obs

    def col(name: str) -> np.ndarray:
        return np.concatenate([panel[name].to_numpy(dtype=np.float64), np.full(pad, np.nan)])

    o, h, lo, c, vix = col("o"), col("h"), col("l"), col("c"), col("vix")
    special = list(panel["special"]) + [False] * pad
    r = log_returns(c)
    rv_var = rolling_mean(r**2, cfg.rv_window)  # at index i: returns up to i
    ewma = ewma_var(r, cfg.ewma_lambda)  # at index i: returns up to i-1 (a forecast for i)
    gap = np.concatenate(([np.nan], np.log(o[1:] / c[:-1])))
    v = np.where(np.isfinite(gap), gap**2, 0.0) + garman_klass_var(o, h, lo, c)
    v[n_obs:] = np.nan
    har = har_forecast(v, min_train=cfg.har_min_train, refit_every=cfg.har_refit_every)
    targets = [all_dates[i] for i in range(1, len(all_dates)) if not special[i]]
    exp = expiry_contexts(targets, rules, calendar)
    lo_t, hi_t = cfg.vol_terciles
    rows: list[dict[str, Any]] = []
    for i in range(1, len(all_dates)):
        if special[i]:
            continue
        t, p = all_dates[i], i - 1
        if dates[p] >= t:
            raise LookaheadError(f"forecast for {t} would use {dates[p]}")
        cal_days = (t - dates[p]).days
        vp = vix[p]
        sig_cal = vp / 100 * math.sqrt(cal_days / CALENDAR_DAYS) if np.isfinite(vp) else math.nan
        sig_trd = vp / 100 * math.sqrt(1 / TRADING_DAYS) if np.isfinite(vp) else math.nan
        pct = _pctl(vix, p, cfg.regime_lookback)
        regime = None if not np.isfinite(pct) else ("LOW" if pct < lo_t else "HIGH" if pct > hi_t else "MID")
        sig_rv = math.sqrt(rv_var[p]) if np.isfinite(rv_var[p]) else math.nan
        w = cfg.trend_window
        tz = (math.log(c[p] / c[p - w]) / (sig_rv * math.sqrt(w))) if p >= w and sig_rv > 0 else math.nan
        trend = None if not np.isfinite(tz) else ("UP" if tz > cfg.trend_z else "DOWN" if tz < -cfg.trend_z
                                                  else "RANGE")
        e = exp[t]
        issues = []
        if not np.isfinite(vp):
            issues.append(f"no India VIX close for {dates[p]}")
        if special[p]:
            issues.append(f"previous session {dates[p]} was a special session")
        live = i >= n_obs and now < live_deadline(t, cfg.live_margin_minutes)
        rows.append({
            "session_date": t.isoformat(), "prev_date": dates[p].isoformat(), "cal_days": cal_days,
            "prev_close": float(c[p]), "vix_prev": _f(vp), "sigma_vix_cal": _f(sig_cal), "sigma_vix_trd": _f(sig_trd),
            "sigma_atm_iv": _atm_sigma(store, underlying, dates[p], t, cal_days) if store is not None else None,
            "sigma_rv20": _f(sig_rv), "sigma_ewma": _f(math.sqrt(ewma[i]) if np.isfinite(ewma[i]) else math.nan),
            "sigma_har": _f(math.sqrt(har[i]) if np.isfinite(har[i]) else math.nan),
            "vix_pctl": _f(pct), "vix_chg5": _f(math.log(vp / vix[p - 5]) if p >= 5 and np.isfinite(vix[p - 5])
                                                and np.isfinite(vp) else math.nan),
            "vol_regime": regime, "trend_z": _f(tz), "trend_regime": trend, "dte": e.dte,
            "is_expiry": None if e.is_expiry is None else int(e.is_expiry),
            "expiry_shifted": None if e.shifted is None else int(e.shifted),
            "is_monthly_expiry": None if e.monthly is None else int(e.monthly),
            "data_issue": "; ".join(issues) or None, "method_version": cfg.method_version,
            "recorded_live": int(live), "created_at": now.isoformat(),
        })
    return rows


def build_outcomes(panel: pd.DataFrame, cfg: ForecastSection, *, now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    recs = panel.to_dict("records")
    for prev, cur in zip(recs[:-1], recs[1:], strict=True):
        if cur["special"]:
            continue
        o, h, lo, c = (float(cur[k]) for k in ("o", "h", "l", "c"))
        pc = float(prev["c"])
        rv5 = cur.get("rv5")
        nmin = cur.get("n_minutes")
        rows.append({
            "session_date": cur["session_date"].isoformat(), "open": o, "high": h, "low": lo, "close": c,
            "prev_close": pc, "r_cc": math.log(c / pc), "r_gap": math.log(o / pc),
            "gap_reliable": int(cur["session_date"] >= cfg.reliable_open_from), "r_oc": math.log(c / o),
            "ln_hl": math.log(h / lo), "park_var": float(parkinson_var(h, lo)),
            "gk_var": float(garman_klass_var(o, h, lo, c)),
            "rv5_var": _f(rv5), "n_minutes": None if nmin is None or not np.isfinite(nmin) else int(nmin),
            "created_at": now.isoformat(),
        })
    return rows


def next_session_after(calendar: SessionCalendar, last: date) -> date:
    d = last + timedelta(days=1)
    for _ in range(14):
        if calendar.is_trading_day(d):
            return d
        d += timedelta(days=1)
    raise LookupError(f"no trading day within 14 days after {last}")


__all__ = ["SESSION_CLOSE", "ExpiryContext", "LookaheadError", "build_forecasts", "build_outcomes",
           "expiry_contexts", "har_features", "har_forecast", "live_deadline", "next_session_after"]
