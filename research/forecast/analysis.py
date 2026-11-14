"""Forecast-quality analysis: implied vs realised NIFTY movement.

Everything here reads the stored forecast and outcome rows and returns plain
dictionaries (JSON-ready). Main definitions:

* ``z = r_cc / sigma``: the close-to-close log return in forecast standard
  deviations. A well-calibrated forecast has ~68.3 % of |z| <= 1 and ~95.4 %
  <= 2 IF returns were normal; real returns are fat-tailed, so the variance
  ratio is the cleaner bias measure.
* variance ratio ``VR = mean(r_cc**2) / mean(sigma**2)``: 1 = unbiased,
  < 1 = the forecast (e.g. VIX) priced MORE movement than happened.
* VRP (volatility risk premium, ex post): India VIX vs the volatility realised
  over the next 30 calendar days (matched horizon), in volatility points.
* overnight share: ``mean(gap**2) / (mean(gap**2) + mean(open-to-close**2))``
  from 2011 (earlier NIFTY opens are unreliable).

Regime labels are the PROVISIONAL R2 buckets (R3 builds the validated engine).
Bucket edges below (gap size, VIX change) are reporting bins, not trading rules.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from research.store import ResearchStore
from volatility import CALENDAR_DAYS, TRADING_DAYS
from volatility.calibration import (
    band_hit_rate,
    block_bootstrap_ratio_ci,
    exceedance_clustering,
    excess_kurtosis,
    mincer_zarnowitz,
    qlike,
    variance_ratio,
)
from volatility.expected_move import EXPECTED_RANGE_FACTOR
from volatility.realised import close_to_close_vol, garman_klass_var, parkinson_var, rolling_mean, yang_zhang_var
from volatility.surface import iv_rank, percentile_rank

FORECASTERS = ("sigma_vix_cal", "sigma_vix_trd", "sigma_rv20", "sigma_ewma", "sigma_har")
LABELS = {"sigma_vix_cal": "India VIX (calendar time)", "sigma_vix_trd": "India VIX (trading time)",
          "sigma_rv20": "Trailing 20-day realised", "sigma_ewma": "EWMA (0.94)", "sigma_har": "HAR-log",
          "sigma_atm_iv": "ATM IV (chain snapshot)"}
GAP_BINS = (0.0, 0.5, 1.0, 2.0, math.inf)
VIX_CHANGE_BINS = (-math.inf, -0.10, 0.10, math.inf)


def _r(x: float, nd: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


def load_joined(store: ResearchStore) -> pd.DataFrame:
    df = pd.read_sql("SELECT f.*, o.open, o.high, o.low, o.close, o.r_cc, o.r_gap, o.gap_reliable, o.r_oc, o.ln_hl,"
                     " o.park_var, o.gk_var, o.rv5_var, o.n_minutes FROM vol_forecasts f"
                     " JOIN vol_outcomes o USING (session_date) ORDER BY session_date", store.con)
    df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
    numeric = [c for c in df.columns if c.startswith("sigma_")] + [
        "vix_prev", "vix_pctl", "vix_chg5", "trend_z", "dte", "r_cc", "r_gap", "r_oc", "ln_hl", "park_var", "gk_var",
        "rv5_var", "n_minutes"]
    for col in numeric:  # an all-NULL column (e.g. no 1-minute data) would otherwise be an object column
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


def summarize(df: pd.DataFrame, sigma: str, *, block: int, boot: bool = True) -> dict[str, Any]:
    d = df[np.isfinite(df[sigma].astype(float)) & (df[sigma].astype(float) > 0)]
    r2, s2 = d["r_cc"].to_numpy() ** 2, d[sigma].to_numpy(dtype=float) ** 2
    z = d["r_cc"].to_numpy() / d[sigma].to_numpy(dtype=float)
    out: dict[str, Any] = {"n": int(len(d))}
    if len(d) < 30:
        return out
    vr = variance_ratio(r2, s2)
    ci = block_bootstrap_ratio_ci(r2, s2, block=block) if boot else (math.nan, math.nan)
    out.update({
        "variance_ratio": _r(vr), "variance_ratio_ci95": [_r(ci[0]), _r(ci[1])],
        "vol_ratio": _r(math.sqrt(vr)) if math.isfinite(vr) else None,
        "hit_1sd": band_hit_rate(z, 1).as_dict(), "hit_2sd": band_hit_rate(z, 2).as_dict(),
        "share_abs_z_gt_3": _r(float(np.mean(np.abs(z) > 3))),
        "median_abs_z": _r(float(np.median(np.abs(z)))),  # normal: 0.6745
        "down_2sd": _r(float(np.mean(z < -2))), "up_2sd": _r(float(np.mean(z > 2))),
    })
    return out


def _bucket_table(df: pd.DataFrame, key: pd.Series, sigma: str, *, block: int) -> list[dict[str, Any]]:
    rows = []
    for name, g in df.groupby(key, sort=True, observed=True):
        s = summarize(g, sigma, block=block, boot=len(g) >= 4 * block)
        if s["n"] >= 30:
            rows.append({"bucket": str(name), "n": s["n"], "variance_ratio": s.get("variance_ratio"),
                         "ci95": s.get("variance_ratio_ci95"), "hit_1sd": s["hit_1sd"]["rate"],
                         "hit_2sd": s["hit_2sd"]["rate"], "share_abs_z_gt_3": s["share_abs_z_gt_3"]})
    return rows


def _dte_bucket(x: Any) -> str | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return str(int(x)) if int(x) < 4 else "4+"


def weekend_multiple(df: pd.DataFrame, *, n_boot: int = 1000, seed: int = 11) -> dict[str, Any]:
    """How many 'normal days' of variance a weekend holds, measured WITHOUT
    any forecast: mean squared close-to-close return after a weekend
    (cal_days == 3) / after a normal night (cal_days == 1). Calendar time
    assumes 3, trading time assumes 1. IID bootstrap 95 % interval."""
    a = (df.loc[df["cal_days"] == 3, "r_cc"] ** 2).to_numpy()
    b = (df.loc[df["cal_days"] == 1, "r_cc"] ** 2).to_numpy()
    if len(a) < 30 or len(b) < 30:
        return {"n_weekend": int(len(a)), "n_normal": int(len(b))}
    rng = np.random.default_rng(seed)
    boots = [rng.choice(a, len(a)).mean() / rng.choice(b, len(b)).mean() for _ in range(n_boot)]
    lo, hi = np.quantile(boots, [0.025, 0.975])
    return {"n_weekend": int(len(a)), "n_normal": int(len(b)), "multiple": _r(float(a.mean() / b.mean()), 3),
            "ci95": [_r(float(lo), 3), _r(float(hi), 3)], "calendar_time_assumes": 3, "trading_time_assumes": 1}


def calibration(df: pd.DataFrame, *, block: int) -> dict[str, Any]:
    v = df[df["sigma_vix_cal"].notna()].copy()
    v["z"] = v["r_cc"] / v["sigma_vix_cal"]
    zz = v["z"].to_numpy()
    cond, base, n_exc = exceedance_clustering(zz, 2)
    worst = v.reindex(v["z"].abs().sort_values(ascending=False).index[:12])
    weekend = {}
    for conv in ("sigma_vix_cal", "sigma_vix_trd"):
        weekend[conv] = {lab: summarize(v[sel], conv, block=block)
                         for lab, sel in (("next_day", v["cal_days"] == 1), ("after_weekend", v["cal_days"] == 3),
                                          ("after_holiday_gap", v["cal_days"] >= 4))}
    weekly = v[v["dte"].notna()]
    gap_ok = v[v["gap_reliable"] == 1]
    gap_z = (gap_ok["r_gap"].abs() / gap_ok["sigma_vix_cal"])
    return {
        "period": [str(v["session_date"].iloc[0]), str(v["session_date"].iloc[-1])],
        "overall": {conv: summarize(v, conv, block=block) for conv in ("sigma_vix_cal", "sigma_vix_trd")},
        "weekend_convention": weekend,
        "weekend_variance_multiple": weekend_multiple(v),
        "excess_kurtosis_z": _r(excess_kurtosis(zz), 2),
        "clustering_2sd": {"p_next_given_today": _r(cond), "unconditional": _r(base), "exceedances": n_exc},
        "largest_surprises": [{"date": str(r["session_date"]), "z": _r(float(r["z"]), 2),
                               "move_pct": _r(100 * float(r["r_cc"]), 2), "vix_prev": _r(float(r["vix_prev"]), 2)}
                              for r in worst.to_dict("records")],
        "by_year": _bucket_table(v, v["session_date"].map(lambda d: d.year), "sigma_vix_cal", block=block),
        "by_vol_regime": _bucket_table(v, v["vol_regime"], "sigma_vix_cal", block=block),
        "by_trend_regime": _bucket_table(v, v["trend_regime"], "sigma_vix_cal", block=block),
        "by_vix_change_5d": _bucket_table(v, pd.cut(v["vix_chg5"], VIX_CHANGE_BINS,
                                                    labels=["falling >10%", "flat", "rising >10%"]),
                                          "sigma_vix_cal", block=block),
        "by_weekday": _bucket_table(v, v["session_date"].map(lambda d: d.strftime("%a")), "sigma_vix_cal",
                                    block=block),
        "by_sessions_to_expiry": _bucket_table(weekly, weekly["dte"].map(_dte_bucket), "sigma_vix_cal", block=block),
        "expiry_days": {
            "all_expiry_days": summarize(weekly[weekly["is_expiry"] == 1], "sigma_vix_cal", block=block),
            "holiday_shifted_expiry_days": summarize(weekly[(weekly["is_expiry"] == 1)
                                                            & (weekly["expiry_shifted"] == 1)], "sigma_vix_cal",
                                                     block=block, boot=False),
            "monthly_expiry_days": summarize(weekly[(weekly["is_expiry"] == 1) & (weekly["is_monthly_expiry"] == 1)],
                                             "sigma_vix_cal", block=block),
            "weekly_era_from": str(weekly["session_date"].iloc[0]) if len(weekly) else None,
        },
        "by_gap_size": _bucket_table(gap_ok, pd.cut(gap_z, GAP_BINS, right=False,
                                                    labels=["<0.5 sd", "0.5-1 sd", "1-2 sd", ">=2 sd"]),
                                     "sigma_vix_cal", block=block),
    }


def overnight_intraday(df: pd.DataFrame) -> dict[str, Any]:
    g = df[df["gap_reliable"] == 1]
    gap2, oc2 = g["r_gap"] ** 2, g["r_oc"] ** 2
    share = float(gap2.mean() / (gap2.mean() + oc2.mean()))
    by_year = [{"year": int(y), "overnight_share": _r(float(a["r_gap"].pow(2).mean()
                                                            / (a["r_gap"].pow(2).mean() + a["r_oc"].pow(2).mean())))}
               for y, a in g.groupby(g["session_date"].map(lambda d: d.year))]
    v = g[g["sigma_vix_cal"].notna()]
    intraday_sigma = v["sigma_vix_cal"] * math.sqrt(1 - share)
    range_ratio = float(v["ln_hl"].mean() / (EXPECTED_RANGE_FACTOR * intraday_sigma).mean())
    oc_vr = variance_ratio(v["r_oc"] ** 2, intraday_sigma**2)
    return {"period_from": str(g["session_date"].iloc[0]), "overnight_share": _r(share),
            "by_year": by_year,
            "intraday_range_ratio": _r(range_ratio),
            "open_to_close_variance_ratio_vs_vix_intraday_share": _r(oc_vr),
            "note": "range ratio = mean ln(H/L) / (Brownian expected range of the VIX-implied intraday sigma); "
                    "1 = VIX priced the intraday range correctly. Observed highs/lows understate a continuous "
                    "range slightly."}


def compare_forecasters(df: pd.DataFrame) -> dict[str, Any]:
    v = df.dropna(subset=list(FORECASTERS)).copy()
    gk_proxy = np.where(v["gap_reliable"] == 1, v["r_gap"] ** 2 + v["gk_var"], np.nan)
    rv_proxy = np.where(v["rv5_var"].notna() & (v["gap_reliable"] == 1), v["r_gap"] ** 2 + v["rv5_var"], np.nan)
    proxies = {"squared_return": v["r_cc"].to_numpy() ** 2, "gap2_plus_garman_klass": gk_proxy,
               "gap2_plus_5min_rv": rv_proxy}
    out: dict[str, Any] = {"period": [str(v["session_date"].iloc[0]), str(v["session_date"].iloc[-1])], "proxies": {}}
    for pname, p in proxies.items():
        ok = np.isfinite(p)
        res = {}
        for f in FORECASTERS:
            h = v[f].to_numpy(dtype=float) ** 2
            mz = mincer_zarnowitz(p[ok], h[ok])
            res[f] = {"qlike": _r(qlike(p[ok], h[ok]), 5), "bias_ratio": _r(variance_ratio(p[ok], h[ok])),
                      "mz_b": _r(mz.b, 3), "mz_r2": _r(mz.r2, 3)}
        def loss(name: str, res: dict[str, dict[str, float | None]] = res) -> float:
            q = res[name]["qlike"]
            return math.inf if q is None else q

        ranked = sorted(res, key=loss)
        out["proxies"][pname] = {"n": int(ok.sum()), "from": str(v["session_date"][ok].iloc[0]) if ok.any() else None,
                                 "ranking_by_qlike": ranked, "metrics": res}
    years = []
    for y, g in v.groupby(v["session_date"].map(lambda d: d.year)):
        p = g["r_cc"].to_numpy() ** 2
        losses = {f: qlike(p, g[f].to_numpy(dtype=float) ** 2) for f in FORECASTERS}
        years.append({"year": int(str(y)), "best": min(losses, key=lambda k: losses[k])})
    wins: dict[str, int] = {}
    for yr in years:
        best = str(yr["best"])
        wins[best] = wins.get(best, 0) + 1
    out["yearly_winner_counts_squared_return"] = wins
    return out


def vrp(df: pd.DataFrame, *, horizon_days: int = 30) -> dict[str, Any]:
    """Ex-post volatility risk premium: VIX vs realised over the next
    ``horizon_days`` calendar days (sessions strictly after t-1's close)."""
    dates: list[date] = list(df["session_date"])
    prevs = [date.fromisoformat(str(x)) for x in df["prev_date"]]
    vix = df["vix_prev"].to_numpy(dtype=float)
    regs = [None if x is None or (isinstance(x, float) and math.isnan(x)) else str(x) for x in df["vol_regime"]]
    r2 = df["r_cc"].to_numpy(dtype=float) ** 2
    csum = np.concatenate(([0.0], np.cumsum(r2)))
    last_date = dates[-1]
    spreads: list[float] = []
    regimes: list[str | None] = []
    made: list[date] = []
    j = 0
    for i in range(len(dates)):
        if not np.isfinite(vix[i]):
            continue
        start = prevs[i]
        end = start + timedelta(days=horizon_days)
        if end > last_date:
            break
        j = max(j, i)
        while j < len(dates) and dates[j] <= end:
            j += 1
        realised_var = csum[j] - csum[i]
        realised_vol = 100 * math.sqrt(realised_var * CALENDAR_DAYS / horizon_days)
        spreads.append(float(vix[i]) - realised_vol)
        regimes.append(regs[i])
        made.append(start)
    s = np.array(spreads)
    by_regime = {}
    for reg in ("LOW", "MID", "HIGH"):
        x = s[np.array([r == reg for r in regimes])]
        if len(x):
            by_regime[reg] = {"n": int(len(x)), "mean": _r(float(x.mean()), 2), "median": _r(float(np.median(x)), 2),
                              "share_positive": _r(float(np.mean(x > 0)), 3)}
    pct = np.percentile(s, [5, 25, 50, 75, 95]) if len(s) else []
    return {"horizon_calendar_days": horizon_days, "n": int(len(s)),
            "from": str(made[0]) if made else None, "to": str(made[-1]) if made else None,
            "mean_vol_points": _r(float(s.mean()), 2) if len(s) else None,
            "share_vix_above_realised": _r(float(np.mean(s > 0)), 3) if len(s) else None,
            "percentiles_5_25_50_75_95": [_r(float(x), 2) for x in pct],
            "worst_5pct_mean": _r(float(s[s <= np.percentile(s, 5)].mean()), 2) if len(s) else None,
            "by_vol_regime": by_regime}


def current_state(df: pd.DataFrame, store: ResearchStore, nifty_key: str, vix_key: str,
                  calib: dict[str, Any] | None = None) -> dict[str, Any]:
    d = pd.read_sql("SELECT session_date, open, high, low, close FROM daily_candles WHERE instrument_key = ?"
                    " AND complete = 1 ORDER BY session_date", store.con, params=(nifty_key,))
    vx = pd.read_sql("SELECT session_date, close FROM daily_candles WHERE instrument_key = ? AND complete = 1"
                     " ORDER BY session_date", store.con, params=(vix_key,))
    o, h, lo, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    rv = {f"rv{n}": _r(100 * float(close_to_close_vol(c, n)[-1]), 2) for n in (5, 10, 20, 30, 60)}
    park20 = 100 * math.sqrt(float(rolling_mean(parkinson_var(h, lo), 20)[-1]) * TRADING_DAYS)
    gk20 = 100 * math.sqrt(float(rolling_mean(garman_klass_var(o, h, lo, c), 20)[-1]) * TRADING_DAYS)
    yz20 = 100 * math.sqrt(float(yang_zhang_var(o, h, lo, c, 20)[-1]) * TRADING_DAYS)
    vix_hist = vx["close"].to_numpy(dtype=float)
    vix_now = float(vix_hist[-1])
    year = vix_hist[-253:-1]
    rv20_series = 100 * close_to_close_vol(c, 20)
    vixd = dict(zip(vx["session_date"], vx["close"], strict=True))
    spread_hist = np.array([vixd[s] - r for s, r in zip(d["session_date"], rv20_series, strict=True)
                            if s in vixd and np.isfinite(r)])
    spread_now = vix_now - (rv["rv20"] or math.nan)
    nxt = pd.read_sql("SELECT * FROM vol_forecasts ORDER BY session_date DESC LIMIT 1", store.con).iloc[0]
    out: dict[str, Any] = {
        "as_of": str(d["session_date"].iloc[-1]), "nifty_close": _r(float(c[-1]), 2), "india_vix": _r(vix_now, 2),
        "vix_percentile_1y": _r(percentile_rank(year, vix_now), 3), "vix_rank_1y": _r(iv_rank(year, vix_now), 3),
        "realised_vol_close_to_close_pct": rv, "parkinson20_pct": _r(park20, 2), "garman_klass20_pct": _r(gk20, 2),
        "yang_zhang20_pct": _r(yz20, 2), "vix_minus_rv20": _r(spread_now, 2),
        "vix_minus_rv20_percentile": _r(percentile_rank(spread_hist, spread_now), 3),
        "next_session": {"date": nxt["session_date"], "recorded_live": bool(nxt["recorded_live"]),
                         "calendar_days": int(nxt["cal_days"]), "regime": nxt["vol_regime"],
                         "trend": nxt["trend_regime"], "sessions_to_expiry": None if pd.isna(nxt["dte"])
                         else int(nxt["dte"]),
                         "sigma_pct": {f: _r(100 * float(nxt[f]), 3) for f in FORECASTERS if pd.notna(nxt[f])},
                         "expected_move_points_1sd_vix_cal": _r(float(c[-1] * nxt["sigma_vix_cal"]), 0)
                         if pd.notna(nxt["sigma_vix_cal"]) else None},
    }
    if calib is not None and pd.notna(nxt["sigma_vix_cal"]):
        days = int(nxt["cal_days"])
        bucket = "next_day" if days == 1 else "after_weekend" if days == 3 else "after_holiday_gap"
        vr = calib["weekend_convention"]["sigma_vix_cal"][bucket].get("variance_ratio")
        if vr:
            raw_move = float(c[-1] * nxt["sigma_vix_cal"])
            out["next_session"]["calibrated_move_points_1sd"] = _r(raw_move * math.sqrt(vr), 0)
            out["next_session"]["calibration_basis"] = (f"VIX calendar-time move x sqrt(historical variance ratio "
                                                        f"{vr} for '{bucket}' sessions); a description of the past, "
                                                        "not a guarantee")
    snaps = store.count("chain_snapshots")
    issues = [f"{int(df['sigma_vix_cal'].isna().sum() - (df['session_date'] < date(2009, 3, 3)).sum())} sessions "
              "since March 2009 without a VIX forecast (missing VIX closes)"]
    if snaps == 0:
        issues.append("no option-chain snapshots yet: ATM IV, skew and term structure are not available "
                      "(run `python -m research chain-snapshot` daily)")
    out["data_quality"] = issues
    return out


def full_report(store: ResearchStore, nifty_key: str, vix_key: str, *, block: int) -> dict[str, Any]:
    df = load_joined(store)
    calib = calibration(df, block=block)
    return {"calibration": calib, "overnight_intraday": overnight_intraday(df),
            "forecasters": compare_forecasters(df), "vrp": vrp(df),
            "current": current_state(df, store, nifty_key, vix_key, calib)}


__all__ = ["FORECASTERS", "LABELS", "calibration", "compare_forecasters", "current_state", "full_report",
           "load_joined", "overnight_intraday", "summarize", "vrp"]
