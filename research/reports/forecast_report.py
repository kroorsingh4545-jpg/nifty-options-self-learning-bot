"""Markdown rendering of ``research.forecast.analysis.full_report``."""

from __future__ import annotations

from typing import Any

from research.forecast.analysis import LABELS


def _pct(x: Any, nd: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{nd}f} %"


def _num(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _ci(ci: Any) -> str:
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:.2f}-{ci[1]:.2f}"


def _bucket_rows(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |",
           "|---|---:|---|---:|---:|---:|"]
    out += [f"| {r['bucket']} | {r['n']} | {_num(r['variance_ratio'])} ({_ci(r['ci95'])}) | {_pct(r['hit_1sd'])} | "
            f"{_pct(r['hit_2sd'])} | {_pct(r['share_abs_z_gt_3'], 2)} |" for r in rows]
    return out


def render_markdown(rep: dict[str, Any]) -> str:
    c, o, f, v, cur = rep["calibration"], rep["overnight_intraday"], rep["forecasters"], rep["vrp"], rep["current"]
    cal = c["overall"]["sigma_vix_cal"]
    trd = c["overall"]["sigma_vix_trd"]
    wm = c["weekend_variance_multiple"]
    L: list[str] = []
    L += ["# Forecast vs reality: India VIX against realised NIFTY movement", "",
          f"Sessions {c['period'][0]} .. {c['period'][1]} (every session with a previous-day India VIX close). "
          "Each forecast uses only data available before the session opened.", "",
          "**How to read this.** *Variance ratio* = realised variance / predicted variance: 1.00 means the "
          "forecast was right on average, below 1 means it predicted MORE movement than happened. *Inside 1 sd* "
          "is the share of sessions whose close-to-close move stayed within the predicted one-standard-deviation "
          "range; a normal distribution would give 68.3 % (95.4 % for 2 sd, 0.27 % beyond 3 sd). An expected-move "
          "range is a description, not a guaranteed probability.", ""]

    L += ["## 1. Overall calibration of India VIX", "",
          "| convention | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |"
          " median abs z (normal 0.67) |", "|---|---:|---|---:|---:|---:|---:|"]
    for name, s in (("calendar time (VIX/100 x sqrt(days/365))", cal), ("trading time (VIX/100 / sqrt(252))", trd)):
        L.append(f"| {name} | {s['n']} | {_num(s['variance_ratio'])} ({_ci(s['variance_ratio_ci95'])}) | "
                 f"{_pct(s['hit_1sd']['rate'])} | {_pct(s['hit_2sd']['rate'])} | {_pct(s['share_abs_z_gt_3'], 2)} | "
                 f"{_num(s['median_abs_z'])} |")
    L += ["", f"Tails: excess kurtosis of the standardised move {c['excess_kurtosis_z']} (normal 0). Down moves beyond "
          f"2 sd {_pct(cal['down_2sd'], 2)} vs up moves {_pct(cal['up_2sd'], 2)}. After a beyond-2-sd session the "
          f"next session is beyond 2 sd {_pct(c['clustering_2sd']['p_next_given_today'])} of the time vs "
          f"{_pct(c['clustering_2sd']['unconditional'])} unconditionally (surprises cluster).", ""]

    L += ["## 2. Weekends: calendar time or trading time?", "",
          f"Without any forecast: a close-to-close move across a weekend has **{_num(wm.get('multiple'))}x** the "
          f"variance of a normal overnight move (95 % CI {_ci(wm.get('ci95'))}; {wm.get('n_weekend')} weekend vs "
          f"{wm.get('n_normal')} normal sessions). Calendar time assumes 3x, trading time 1x.", "",
          "| convention | after a normal night | after a weekend | after a longer holiday gap |", "|---|---|---|---|"]
    for conv, lab in (("sigma_vix_cal", "calendar time"), ("sigma_vix_trd", "trading time")):
        w = c["weekend_convention"][conv]
        cells = (f"VR {_num(w[k].get('variance_ratio'))} ({_ci(w[k].get('variance_ratio_ci95'))}), n={w[k]['n']}"
                 for k in ("next_day", "after_weekend", "after_holiday_gap"))
        L.append(f"| {lab} | " + " | ".join(cells) + " |")
    L += [""]

    L += ["## 3. Where the forecast errors are largest", ""]
    for title, key in (("By provisional volatility regime (VIX percentile vs the past year)", "by_vol_regime"),
                       ("By provisional trend regime (20-session move vs its expected size)", "by_trend_regime"),
                       ("By 5-session change in India VIX", "by_vix_change_5d"),
                       ("By weekday", "by_weekday"),
                       ("By overnight gap size (in predicted sd; 2011 onwards)", "by_gap_size"),
                       ("By year", "by_year")):
        L += [f"### {title}", ""] + _bucket_rows(c[key]) + [""]

    e = c["expiry_days"]
    L += ["## 4. Expiry", "", f"Weekly NIFTY expiries from {e['weekly_era_from']} (rule calendar, holiday shifts "
          "applied; Thursday until 28 Aug 2025, Tuesday from 2 Sep 2025).", "",
          "### By sessions to the next weekly expiry (0 = expiry day)", ""] + _bucket_rows(c["by_sessions_to_expiry"])
    L += ["", "| expiry days | sessions | variance ratio (95 % CI) | inside 1 sd |", "|---|---:|---|---:|"]
    for lab, k in (("all expiry days", "all_expiry_days"), ("monthly expiry days", "monthly_expiry_days"),
                   ("holiday-shifted expiry days", "holiday_shifted_expiry_days")):
        s = e[k]
        L.append(f"| {lab} | {s['n']} | {_num(s.get('variance_ratio'))} ({_ci(s.get('variance_ratio_ci95'))}) | "
                 f"{_pct(s['hit_1sd']['rate']) if 'hit_1sd' in s else 'too few sessions'} |")
    L += ["", "Caution: sessions-to-expiry overlaps with weekday (before Sep 2025, '4+' was mostly Fridays), so "
          "these two tables are not independent evidence.", ""]

    L += ["## 5. Overnight vs intraday", "",
          f"From {o['period_from']}: **{_pct(o['overnight_share'])} of daily NIFTY variance happens between the close "
          "and the next open** (when an intraday strategy holds nothing). Open-to-close variance vs the VIX-implied "
          f"intraday share: ratio {_num(o['open_to_close_variance_ratio_vs_vix_intraday_share'])}; intraday "
          f"high-low range vs the VIX-implied Brownian range: ratio {_num(o['intraday_range_ratio'])}.", "",
          "| year | overnight share |", "|---|---:|"] + \
         [f"| {y['year']} | {_pct(y['overnight_share'])} |" for y in o["by_year"]] + [""]

    L += ["## 6. Which forecast describes the next session best?", "",
          "Lower QLIKE is better (Patton's robust loss). *Bias* = mean realised / mean predicted variance. "
          "*MZ b*: slope of realised on predicted (1 = right scale); *MZ R2*: share of the variation explained.", ""]
    for pname, lab in (("squared_return", "proxy: squared close-to-close return"),
                       ("gap2_plus_garman_klass", "proxy: gap squared + Garman-Klass (2011+)"),
                       ("gap2_plus_5min_rv", "proxy: gap squared + 5-minute realised variance (2022+)")):
        p = f["proxies"][pname]
        L += [f"### {lab} ({p['n']} sessions from {p['from']})", "",
              "| forecast | QLIKE | bias | MZ b | MZ R2 |", "|---|---:|---:|---:|---:|"]
        for k in p["ranking_by_qlike"]:
            m = p["metrics"][k]
            L.append(f"| {LABELS[k]} | {_num(m['qlike'], 4)} | {_num(m['bias_ratio'])} | {_num(m['mz_b'])} | "
                     f"{_num(m['mz_r2'])} |")
        L += [""]
    wins = ", ".join(f"{LABELS[k]} {n}" for k, n in sorted(f["yearly_winner_counts_squared_return"].items(),
                                                            key=lambda kv: -kv[1]))
    L += [f"Best forecast per calendar year (squared-return proxy): {wins}.", ""]

    L += ["## 7. Volatility risk premium (India VIX vs the next 30 days)", "",
          f"{v['n']} sessions {v['from']} .. {v['to']}. India VIX was above the volatility actually realised over "
          f"the next 30 calendar days **{_pct(v['share_vix_above_realised'])}** of the time; average premium "
          f"**{_num(v['mean_vol_points'])} volatility points** (percentiles 5/25/50/75/95: "
          f"{', '.join(_num(x) for x in v['percentiles_5_25_50_75_95'])}). The worst 5 % of months averaged "
          f"{_num(v['worst_5pct_mean'])} points (realised far above implied).", "",
          "| regime | sessions | mean | median | VIX above realised |", "|---|---:|---:|---:|---:|"]
    L += [f"| {k} | {x['n']} | {_num(x['mean'])} | {_num(x['median'])} | {_pct(x['share_positive'])} |"
          for k, x in v["by_vol_regime"].items()] + [""]

    L += ["## 8. Largest surprises (|move| in VIX-implied sd)", "", "| date | z | NIFTY move | VIX before |",
          "|---|---:|---:|---:|"]
    L += [f"| {s['date']} | {s['z']} | {s['move_pct']} % | {s['vix_prev']} |" for s in c["largest_surprises"]]
    L += [""]

    ns = cur["next_session"]
    rv = cur["realised_vol_close_to_close_pct"]
    L += [f"## 9. Current state (as of {cur['as_of']})", "",
          f"NIFTY {cur['nifty_close']}, India VIX {cur['india_vix']} "
          f"(1-year percentile {_pct(cur['vix_percentile_1y'])}, 1-year rank {_pct(cur['vix_rank_1y'])}).", "",
          f"Realised volatility (annualised, close-to-close): 5d {rv['rv5']} %, 10d {rv['rv10']} %, "
          f"20d {rv['rv20']} %, 30d {rv['rv30']} %, 60d {rv['rv60']} %. "
          f"20-day Parkinson {cur['parkinson20_pct']} %, Garman-Klass {cur['garman_klass20_pct']} %, "
          f"Yang-Zhang {cur['yang_zhang20_pct']} %.", "",
          f"VIX minus 20-day realised: {cur['vix_minus_rv20']} points (historical percentile "
          f"{_pct(cur['vix_minus_rv20_percentile'])}).", "",
          f"Next session {ns['date']} ({ns['calendar_days']} calendar day(s) after the last close; forecast "
          f"{'recorded live before the open' if ns['recorded_live'] else 'not live'}): provisional regime "
          f"{ns['regime']}, trend {ns['trend']}, {ns['sessions_to_expiry']} session(s) to the weekly expiry.", "",
          "| forecast | 1 sd move |", "|---|---:|"]
    L += [f"| {LABELS[k]} | {x} % |" for k, x in ns["sigma_pct"].items()]
    L += ["", f"VIX expected move (1 sd, calendar time): {ns['expected_move_points_1sd_vix_cal']} points."]
    if "calibrated_move_points_1sd" in ns:
        L += [f"Historically calibrated: {ns['calibrated_move_points_1sd']} points ({ns['calibration_basis']})."]
    L += ["", "### Data-quality warnings", ""] + [f"- {w}" for w in cur["data_quality"]] + [""]
    return "\n".join(L)


__all__ = ["render_markdown"]
