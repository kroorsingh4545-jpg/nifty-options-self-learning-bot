"""Summaries of shadow outcomes and calibration (pure; text and JSON).

Every number is per ONE LOT, after charges, as if each signal had been traded
alone. With the samples available these are descriptions, not evidence: the
report prints the sample size next to every figure and says when it is too
small for any conclusion (``MIN_SAMPLE``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from statistics import mean, median
from typing import Any

#: below this many filled counterfactual trades a group is "too few to judge"
MIN_SAMPLE = 30


def _group(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    filled = [r for r in rows if r["status"] == "FILLED"]
    nets = [float(r["net"]) for r in filled]
    rs = [float(r["r_net"]) for r in filled if r["r_net"] is not None]
    return {"signals": len(rows), "filled": len(filled), "status": dict(Counter(r["status"] for r in rows)),
            "total_net": round(sum(nets), 2) if nets else 0.0,
            "mean_net": round(mean(nets), 2) if nets else None,
            "mean_r_net": round(mean(rs), 3) if rs else None,
            "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 3) if nets else None,
            "exit_reasons": dict(Counter(r["exit_reason"] for r in filled)),
            "enough_to_judge": len(filled) >= MIN_SAMPLE}


def summarize(rows: Sequence[Mapping[str, Any]], calib: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_strategy = {sid: _group([r for r in rows if r["strategy_id"] == sid])
                   for sid in sorted({str(r["strategy_id"]) for r in rows})}
    approved = [r for r in rows if r["approved"] == 1]
    rejected = [r for r in rows if r["approved"] == 0]
    trades = [c for c in calib if "trade_uid" in c]
    pit = [c["point_in_time"] for c in trades if "point_in_time" in c]
    rep = [c["replay"] for c in trades if "replay" in c]
    logged = [c for c in calib if c.get("logged_risk_per_unit_diff") is not None]

    def med(xs: list[Any]) -> float | None:
        xs = [abs(x) for x in xs if x is not None]
        return round(median(xs), 1) if xs else None

    cal = {
        "trades": len(trades), "simulated": len(pit), "replayed": len(rep),
        "within_tolerance": sum(1 for c in trades if c.get("within_tolerance")),
        "not_simulated": [c["trade_uid"] for c in trades if c.get("judged_on") is None],
        "point_in_time_median_abs_entry_ticks": med([d["entry_ticks"] for d in pit]),
        "point_in_time_median_abs_exit_ticks": med([d["exit_ticks"] for d in pit]),
        "replay_median_abs_entry_ticks": med([d["entry_ticks"] for d in rep]),
        "exit_reason_matches": sum(1 for d in pit if d["exit_reason_match"]),
        "logged_risk_per_unit_median_abs_diff": med([c["logged_risk_per_unit_diff"] for c in logged]),
        "logged_decisions": len(logged),
        "logged_risk_per_unit_exact": sum(1 for c in logged if abs(c["logged_risk_per_unit_diff"]) < 0.005),
        "logged_contract_matches": sum(1 for c in calib if c.get("logged_contract_match") is True),
        "logged_contracts": sum(1 for c in calib if "logged_contract_match" in c),
    }
    return {"all": _group(rows), "approved": _group(approved), "rejected": _group(rejected),
            "by_strategy": by_strategy, "calibration": cal, "min_sample": MIN_SAMPLE}


def render_text(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                calib: Sequence[Mapping[str, Any]]) -> list[str]:
    def money(x: Any) -> str:
        return "n/a" if x is None else f"{x:,.2f}"

    def grp(name: str, g: Mapping[str, Any]) -> str:
        judge = "" if g["enough_to_judge"] else f"  [too few to judge: {g['filled']} < {summary['min_sample']}]"
        return (f"  {name:10s} signals {g['signals']:3d}, filled {g['filled']:3d}, net/lot {money(g['total_net'])}, "
                f"mean {money(g['mean_net'])}, mean R {g['mean_r_net']}, win rate {g['win_rate']}{judge}")

    lines = ["Counterfactual outcomes (1 lot each, after charges, each signal traded alone):",
             grp("all", summary["all"]), grp("approved", summary["approved"]), grp("rejected", summary["rejected"])]
    lines += [grp(sid, g) for sid, g in summary["by_strategy"].items()]
    lines.append("")
    lines.append("signal time          strategy     dir  decided  contract        status   entry   exit  reason"
                 "         net/lot     R")
    for r in rows:
        decided = "approved" if r["approved"] == 1 else ("rejected" if r["approved"] == 0 else "undecided")
        lines.append(f"{str(r['generated_at'])[:19]}  {r['strategy_id']:12s} {r['option_type']:3s}  {decided:8s} "
                     f"{str(r['instrument_key'] or '-'):15s} {r['status']:8s} {money(r['entry_price']):>7s} "
                     f"{money(r['exit_price']):>6s}  {str(r['exit_reason'] or '-'):13s} {money(r['net']):>9s} "
                     f"{'' if r['r_net'] is None else r['r_net']:>6}")
    c = summary["calibration"]
    lines += ["", f"Calibration against real PAPER trades: {c['within_tolerance']} of {c['trades']} reproduced "
                  f"within tolerance ({c['simulated']} simulated, {c['replayed']} replayed from the logged ask)",
              f"  point-in-time sampling error: median |entry| {c['point_in_time_median_abs_entry_ticks']} ticks, "
              f"|exit| {c['point_in_time_median_abs_exit_ticks']} ticks; replay median |entry| "
              f"{c['replay_median_abs_entry_ticks']} ticks; exit reason matched {c['exit_reason_matches']}"]
    if c["not_simulated"]:
        lines.append(f"  not simulated (no recorded quotes): {', '.join(c['not_simulated'])}")
    for x in calib:
        if "trade_uid" not in x or x.get("judged_on") is None:
            continue
        line = f"  {x['trade_uid']}: contract {'ok' if x['contract_match'] else 'DIFFERENT'}, real {x['actual_entry']}"
        line += f" -> {x['actual_exit']} ({x['actual_exit_reason']})"
        for name in ("point_in_time", "replay"):
            d = x.get(name)
            if d:
                line += (f"; {name}: entry {d['entry_ticks']:+} ticks, exit band {d['exit_band']} "
                         f"(outside by {d['exit_outside_band_ticks']} ticks), {d['simulated_exit_reason']}")
        lines.append(line + f" -> {'REPRODUCED' if x['within_tolerance'] else 'NOT reproduced'}")
    lines.append(f"Against the risk manager's log: risk per unit exact for {c['logged_risk_per_unit_exact']} of "
                 f"{c['logged_decisions']} (median |diff| {c['logged_risk_per_unit_median_abs_diff']}); contract "
                 f"matched {c['logged_contract_matches']} of {c['logged_contracts']}")
    return lines


__all__ = ["MIN_SAMPLE", "render_text", "summarize"]
