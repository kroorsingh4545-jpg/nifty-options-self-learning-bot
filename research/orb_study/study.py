"""ORB study E1: hypotheses H1-H9 on the stored history (pure; see the registration).

``run`` takes prepared sessions and the calendar / cost / rule inputs built by
the composition root and returns one JSON-able report. Option results are
ESTIMATED, not real option prices (``research.orb_study.options``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from research.orb_study.data import LAST_BAR, SQUARE_OFF_BAR, Session
from research.orb_study.options import (
    CREDIT,
    DEBIT,
    LONG_ATM,
    STRUCTURES,
    Fills,
    Market,
    OptTrade,
    Pricer,
    approx_long_atm,
    legs_for,
    our_exit_long,
    structure_trade,
)
from research.orb_study.registration import REGISTERED_FINGERPRINT, STUDY_E1, STUDY_VERSION, fingerprint
from research.orb_study.setups import SETUPS, Entry, filtered, our_entries, source_entries, spot_exit
from research.orb_study.stats import apply_bonferroni, pass_rule, summary
from research.regime.orb import OrbRule
from research.shadow.rules import TradeRules

ESTIMATED = "ESTIMATED, not real option prices"
RANGES = (5, 15, 30, 60)
FILTERS = ("A_BREAKOUT_60", "B_RETEST_30", "C_REVERSAL_5")

#: the source's reported numbers (from the owner's reading of the two transcripts; claims, not facts)
SOURCE: dict[str, Any] = {
    "sessions": 1668, "period": "2020-01 .. 2026-09",
    "H1": {"BREAKOUT": {"5": {"total": -742}, "15": {"total": 3290}, "30": {"total": 6739, "win_rate": 49},
                        "60": {"total": 8346, "trades": 1579, "win_rate": 51, "profit_factor": 1.16}},
           "RETEST": {"5": {"total": 6101}, "30": {"total": 7336, "trades": 1381}},
           "REVERSAL": {"5": {"total": 4069, "trades": 883}, "15": {"total": 4030}, "30": {"total": 2889},
                        "60": {"total": 2955, "trades": 342, "win_rate": 52}}},
    "reversal_frequency": {"5": 53, "15": 42, "30": 32, "60": 22},
    "H2": {"A_BREAKOUT_60": {"trades": 648, "total": 7021}, "B_RETEST_30": {"trades": 713, "total": 6487},
           "C_REVERSAL_5": {"trades": 599, "total": 5424, "win_rate": 44}},
}


@dataclass(frozen=True)
class Limits:
    capital: float  # strategy capital
    risk_per_trade: float
    max_daily_loss: float


@dataclass
class _Row:
    e: Entry
    s: Session
    points: float
    exit_m: int
    reason: str


def _days(trades: Sequence[Any]) -> list[date]:
    return [t.day if hasattr(t, "day") else t.e.day for t in trades]


def _opt_summary(trades: Sequence[OptTrade]) -> dict[str, Any]:
    out = summary([t.day for t in trades], [t.net for t in trades])
    out["gross_total"] = round(sum(t.gross for t in trades), 2)
    out["costs_total"] = round(sum(t.costs for t in trades), 2)
    out["exit_reasons"] = {r: sum(1 for t in trades if t.exit_reason == r) for r in sorted({t.exit_reason
                                                                                           for t in trades})}
    out["label"] = ESTIMATED
    return out


def _paired(a: dict[Any, OptTrade], b: dict[Any, OptTrade]) -> tuple[list[date], list[float]]:
    """Per-entry difference a - b on the entries present in both."""
    keys = sorted(set(a) & set(b), key=str)
    return [a[k].day for k in keys], [a[k].net - b[k].net for k in keys]


def _paired_summary(days: list[date], diff: list[float]) -> dict[str, Any]:
    out = summary(days, diff)
    out["label"] = ESTIMATED
    return out


def _key(e: Entry) -> tuple[str, int, int]:
    return (e.day.isoformat(), e.m, e.direction)


def run(sessions: Sequence[Session], *, mk: Market, fills: Fills, our_rules: TradeRules, orb_rule: OrbRule,
        tz: Any, limits: Limits, data: dict[str, Any], progress: Callable[[str], None] | None = None,
        draws: int | None = None) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    kw = {} if draws is None else {"draws": draws}
    tests: dict[str, dict[str, Any]] = {}
    rep: dict[str, Any] = {"study": STUDY_VERSION, "fingerprint": fingerprint(),
                           "fingerprint_matches_registration": fingerprint() == REGISTERED_FINGERPRINT,
                           "data": data, "source": SOURCE, "lot": fills.lot}
    if fingerprint() != REGISTERED_FINGERPRINT:
        raise ValueError("STUDY_E1 does not match its registered fingerprint: register a new version")
    by_day = {s.day: s for s in sessions}
    pricers: dict[tuple[date, int], Pricer] = {}

    def pricer(e: Entry) -> Pricer:
        k = (e.day, e.m)
        if k not in pricers:
            pricers[k] = Pricer(by_day[e.day], e.m, mk)
        return pricers[k]

    # ---------------- H1 / H2 / H3: source rules, spot points, approx long ATM ----------------
    say("H1-H3: source setups")
    rows: dict[tuple[str, int], list[_Row]] = {(st, n): [] for st in SETUPS for n in RANGES}
    break_days = dict.fromkeys(RANGES, 0)
    rev_days = dict.fromkeys(RANGES, 0)
    skipped_range = dict.fromkeys(RANGES, 0)
    for s in sessions:
        for n in RANGES:
            ents = source_entries(s, n)
            if not np.isfinite(s.c[:n]).all():
                skipped_range[n] += 1
            if ents["BREAKOUT"] is not None:
                break_days[n] += 1
                rev_days[n] += int(ents["REVERSAL"] is not None)
            for st, e in ents.items():
                if e is None:
                    continue
                x = spot_exit(s, e, exit_bar=LAST_BAR)
                if x is not None:
                    rows[(st, n)].append(_Row(e, s, x.points(e), x.m, x.reason))
    scale = data["sessions_used"] / SOURCE["sessions"]
    h1: dict[str, Any] = {"scale_to_our_sessions": round(scale, 4), "variants": {}}
    for (st, n), rs in rows.items():
        sm = summary(_days(rs), [r.points for r in rs])
        sm["exit_reasons"] = {k: sum(1 for r in rs if r.reason == k) for k in ("STOP", "TIME")}
        src = SOURCE["H1"].get(st, {}).get(str(n))
        if src:
            sm["source"] = src
            sm["source_total_scaled"] = round(src["total"] * scale, 1)
            if "trades" in src:
                sm["source_mean_per_trade"] = round(src["total"] / src["trades"], 3)
        h1["variants"][f"{st}.{n}"] = sm
    h1["reversal_frequency_pct"] = {str(n): round(100 * rev_days[n] / break_days[n], 1) if break_days[n] else None
                                    for n in RANGES}
    h1["days_with_first_break"] = {str(n): break_days[n] for n in RANGES}
    h1["days_skipped_missing_range_minute"] = {str(n): skipped_range[n] for n in RANGES}
    rep["H1"] = h1

    frows: dict[str, list[_Row]] = {}
    for name in FILTERS:
        rule = STUDY_E1["filters_source"][name]
        frows[name] = [r for r in rows[(rule["setup"], rule["range_minutes"])] if filtered(r.e, rule)]
    rep["H2"] = {}
    for name, rs in frows.items():
        sm = summary(_days(rs), [r.points for r in rs])
        sm["source"] = SOURCE["H2"][name]
        sm["source_total_scaled"] = round(SOURCE["H2"][name]["total"] * scale, 1)
        sm["source_mean_per_trade"] = round(SOURCE["H2"][name]["total"] / SOURCE["H2"][name]["trades"], 3)
        rep["H2"][name] = sm

    rep["H3"] = {"label": ESTIMATED + " (cost basis only; P&L = 0.5 x points x lot)", "variants": {}}
    groups = {**{f"{st}.{n}": rows[(st, n)] for st in SETUPS for n in RANGES}, **frows}
    for name, rs in groups.items():
        days, gross, net, prem = [], [], [], []
        for r in rs:
            p = pricer(r.e)
            if not p.ok:
                continue
            ap = approx_long_atm(r.e, r.points, p, fills)
            days.append(r.e.day)
            gross.append(ap["gross"])
            net.append(ap["net"])
            prem.append(ap["premium"])
        sm = summary(days, net)
        g = float(sum(gross))
        sm["gross_total"] = round(g, 2)
        sm["costs_total"] = round(g - float(sum(net)), 2)
        sm["costs_share_of_gross"] = round((g - float(sum(net))) / g, 3) if g > 0 else None
        sm["median_entry_premium"] = round(float(np.median(prem)), 2) if prem else None
        rep["H3"]["variants"][name] = sm
        tests[f"H3.{name}"] = {"what": f"{name} approx long ATM, after costs", **pass_rule(days, net, **kw)}

    # ---------------- H4 / H5: our ORB, our exit vs their exit ----------------
    say("H4-H5: our ORB")
    ours: list[tuple[Session, Entry]] = [(s, e) for s in sessions for e in our_entries(s, orb_rule, tz)]
    h4: dict[Any, OptTrade] = {}
    h5: dict[Any, OptTrade] = {}
    refused: dict[str, int] = {}
    h5_points: list[float] = []
    h5_days: list[date] = []
    for s, e in ours:
        p = pricer(e)
        if not p.ok:
            refused["NO_PRICE"] = refused.get("NO_PRICE", 0) + 1
            continue
        t = our_exit_long(s, e, p, fills, our_rules, exit_bar=SQUARE_OFF_BAR)
        if isinstance(t, str):
            refused[t] = refused.get(t, 0) + 1
            continue
        h4[_key(e)] = t
        x = spot_exit(s, e, exit_bar=SQUARE_OFF_BAR)
        if x is not None:
            h5[_key(e)] = structure_trade(LONG_ATM, s, e, p, fills, x.m, x.spot, x.reason)
            h5_points.append(x.points(e))
            h5_days.append(e.day)
    rep["H4"] = {"signals": len(ours), "refused": refused, **_opt_summary(list(h4.values()))}
    rep["H5"] = {**_opt_summary(list(h5.values())), "spot_points": summary(h5_days, h5_points)}
    tests["H4"] = {"what": "our ORB, our exit, long ATM", **pass_rule(_days(list(h4.values())),
                                                                      [t.net for t in h4.values()], **kw)}
    tests["H5"] = {"what": "our ORB entries, their exit (15:10), long ATM",
                   **pass_rule(_days(list(h5.values())), [t.net for t in h5.values()], **kw)}
    d, diff = _paired(h5, h4)
    rep["H5_minus_H4"] = _paired_summary(d, diff)
    tests["H5-H4"] = {"what": "their exit minus our exit, same ORB entries", **pass_rule(d, diff, **kw)}

    # ---------------- H6: their 60 m breakout entries with our exit ----------------
    say("H6: their entries, our exit")
    h6: dict[Any, OptTrade] = {}
    h2a: dict[Any, OptTrade] = {}
    for r in frows["A_BREAKOUT_60"]:
        p = pricer(r.e)
        if not p.ok:
            continue
        t = our_exit_long(r.s, r.e, p, fills, our_rules, exit_bar=SQUARE_OFF_BAR)
        if isinstance(t, str):
            continue
        h6[_key(r.e)] = t
        h2a[_key(r.e)] = structure_trade(LONG_ATM, r.s, r.e, p, fills, r.exit_m, r.e.spot + r.e.direction * r.points,
                                         r.reason)
    rep["H6"] = _opt_summary(list(h6.values()))
    rep["H2A_long_atm_their_exit"] = _opt_summary(list(h2a.values()))
    tests["H6"] = {"what": "A_BREAKOUT_60 entries, our exit, long ATM",
                   **pass_rule(_days(list(h6.values())), [t.net for t in h6.values()], **kw)}
    d, diff = _paired(h6, h2a)
    rep["H6_minus_H2A"] = _paired_summary(d, diff)
    tests["H6-H2A"] = {"what": "our exit minus their exit (15:29), A_BREAKOUT_60 entries", **pass_rule(d, diff, **kw)}

    # ---------------- H7 / H8: structures on the filtered setups ----------------
    say("H7-H8: structures")
    rep["H7"], rep["H8"] = {}, {}
    structs: dict[str, dict[str, dict[Any, OptTrade]]] = {}
    for name in FILTERS:
        structs[name] = {st: {} for st in STRUCTURES}
        wing_steps: list[int] = []
        for r in frows[name]:
            p = pricer(r.e)
            if not p.ok:
                continue
            exit_spot = r.e.spot + r.e.direction * r.points
            for st in STRUCTURES:
                structs[name][st][_key(r.e)] = structure_trade(st, r.s, r.e, p, fills, r.exit_m, exit_spot, r.reason)
            legs = legs_for(CREDIT, r.e, p)
            wing_steps.append(int(round(abs(legs[1].strike - legs[0].strike) / mk.strike_step)))
        block: dict[str, Any] = {"label": ESTIMATED}
        for st in STRUCTURES:
            ts = list(structs[name][st].values())
            sm = _opt_summary(ts)
            if ts:
                ml = np.asarray([t.max_loss for t in ts])
                mg = np.asarray([t.margin_est for t in ts])
                worst = min(t.net for t in ts)
                sm["max_loss_at_entry"] = {"median": round(float(np.median(ml)), 0), "p90": round(float(
                    np.percentile(ml, 90)), 0), "max": round(float(np.max(ml)), 0)}
                sm["margin_estimate"] = {"median": round(float(np.median(mg)), 0), "p90": round(float(
                    np.percentile(mg, 90)), 0), "max": round(float(np.max(mg)), 0)}
                sm["share_max_loss_above_risk_per_trade"] = round(float((ml > limits.risk_per_trade).mean()), 3)
                sm["share_max_loss_above_daily_limit"] = round(float((ml > limits.max_daily_loss).mean()), 3)
                sm["worst_trade"] = round(worst, 2)
                sm["trades_losing_more_than_risk_per_trade"] = sum(1 for t in ts if t.net < -limits.risk_per_trade)
                sm["trades_losing_more_than_daily_limit"] = sum(1 for t in ts if t.net < -limits.max_daily_loss)
                sm["fits_capital"] = bool(np.percentile(mg, 90) <= limits.capital)
            block[st] = sm
            tests[f"H7.{name}.{st}"] = {"what": f"{name} {st}, their exit 15:29",
                                        **pass_rule(_days(ts), [t.net for t in ts], **kw)}
        for a, b in ((CREDIT, DEBIT), (CREDIT, LONG_ATM), (DEBIT, LONG_ATM)):
            d, diff = _paired(structs[name][a], structs[name][b])
            block[f"{a}_minus_{b}"] = _paired_summary(d, diff)
            tests[f"H7.{name}.{a}-{b}"] = {"what": f"{name} {a} minus {b}", **pass_rule(d, diff, **kw)}
        if wing_steps:
            w = np.asarray(wing_steps)
            block["wing_strikes_from_atm"] = {"median": float(np.median(w)), "p10": float(np.percentile(w, 10)),
                                              "p90": float(np.percentile(w, 90)), "max": int(np.max(w)),
                                              "share_within_5": round(float((w <= 5).mean()), 3),
                                              "share_within_3": round(float((w <= 3).mean()), 3)}
        rep["H7"][name] = block
        # H8: DTE buckets of the long ATM results (descriptive, low prior)
        longs = list(structs[name][LONG_ATM].values())
        buckets: dict[str, Any] = {}
        for label in ("0", "1", "2", "3", "4+"):
            in_bucket = [t for t in longs if (str(t.dte) if t.dte < 4 else "4+") == label]
            sm = summary(_days(in_bucket), [t.net for t in in_bucket])
            sm["positive_test_years"] = sum(1 for y in ("2022", "2023", "2024", "2025")
                                            if (sm.get("by_year", {}).get(y, {}).get("mean") or 0) > 0)
            buckets[label] = sm
        rep["H8"][name] = {"label": ESTIMATED + "; LOW PRIOR, descriptive only", "buckets": buckets}

    # ---------------- H9: fixed targets vs hold (exit 15:10) ----------------
    say("H9: targets")
    rep["H9"] = {}
    for name in FILTERS:
        block = {"label": ESTIMATED}
        for st in (LONG_ATM, CREDIT):
            hold: dict[Any, OptTrade] = {}
            tgt: dict[int, dict[Any, OptTrade]] = {k: {} for k in range(1, 6)}
            for r in frows[name]:
                p = pricer(r.e)
                if not p.ok:
                    continue
                x = spot_exit(r.s, r.e, exit_bar=SQUARE_OFF_BAR)
                if x is None:
                    continue
                hold[_key(r.e)] = structure_trade(st, r.s, r.e, p, fills, x.m, x.spot, x.reason)
                for k in tgt:
                    xk = spot_exit(r.s, r.e, exit_bar=SQUARE_OFF_BAR, target_r=float(k))
                    if xk is not None:
                        tgt[k][_key(r.e)] = structure_trade(st, r.s, r.e, p, fills, xk.m, xk.spot, xk.reason)
            sub: dict[str, Any] = {"hold": _opt_summary(list(hold.values()))}
            for k, by_entry in tgt.items():
                sub[f"{k}R"] = _opt_summary(list(by_entry.values()))
                d, diff = _paired(hold, by_entry)
                sub[f"hold_minus_{k}R"] = _paired_summary(d, diff)
                tests[f"H9.{name}.{st}.hold-{k}R"] = {"what": f"{name} {st}: hold to stop/15:10 minus {k}R target",
                                                      **pass_rule(d, diff, **kw)}
            block[st] = sub
        rep["H9"][name] = block

    m = apply_bonferroni(tests)
    rep["tests"] = tests
    rep["tests_run"] = m
    rep["passed"] = sorted(k for k, t in tests.items() if t["verdict"] == "PASS")
    rep["weak"] = sorted(k for k, t in tests.items() if t["verdict"] == "WEAK")
    return rep


def _fmt(x: Any) -> str:
    return "-" if x is None else (f"{x:,.1f}" if isinstance(x, float) else str(x))


def render_text(rep: dict[str, Any]) -> list[str]:
    """A short console view; the full report is the JSON."""
    lines = [f"ORB study {rep['study']} (fingerprint matches registration: {rep['fingerprint_matches_registration']})",
             f"data: {rep['data']['first_used']} .. {rep['data']['last_used']}, {rep['data']['sessions_used']} "
             f"sessions used, {len(rep['data']['excluded'])} excluded", "", "H1 spot points (before costs):"]
    for k, v in rep["H1"]["variants"].items():
        lines.append(f"  {k:14} trades {v['trades']:5} total {_fmt(v.get('total')):>10} win {_fmt(v.get('win_rate'))}"
                     f" % PF {_fmt(v.get('profit_factor'))}  source scaled {_fmt(v.get('source_total_scaled'))}")
    lines.append(f"  reversal frequency %: {rep['H1']['reversal_frequency_pct']}")
    lines.append("H2 filtered (spot points):")
    for k, v in rep["H2"].items():
        lines.append(f"  {k:14} trades {v['trades']:5} total {_fmt(v.get('total')):>10} win {_fmt(v.get('win_rate'))}"
                     f" %  source scaled {_fmt(v.get('source_total_scaled'))}")
    lines.append("")
    lines.append(f"tests run: {rep['tests_run']}; PASS {rep['passed']}; WEAK {rep['weak']}")
    for k, t in rep["tests"].items():
        lines.append(f"  {k:34} n {t.get('n', 0):5} mean {_fmt(t.get('mean')):>9} CI {t.get('ci95')} years+ "
                     f"{t.get('positive_years')} -> {t['verdict']}")
    return lines


__all__ = ["ESTIMATED", "FILTERS", "RANGES", "SOURCE", "Limits", "render_text", "run"]
