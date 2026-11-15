"""The pre-registered forward evaluation of the shadow engine (pure).

Exactly as registered (``RULES_V1["evaluation"]``): only signals generated on
or after ``EVALUATION_FROM`` with a TRADE or NO_TRADE verdict and a D2
counterfactual net R count. Primary metric: mean net R of TRADE minus mean net R
of NO_TRADE; it passes when its day-block bootstrap 95 % interval lies above
zero AND the TRADE signals' total net is at least that of all counted signals.
Below the registered sample sizes the status is NOT_YET_EVALUABLE.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import numpy as np

from research.decision.engine import NO_TRADE, TRADE
from research.decision.rules import EVALUATION_FROM, RULES_V1

NOT_YET, FIRST_LOOK, FINAL = "NOT_YET_EVALUABLE", "FIRST_LOOK", "FINAL"


def _ci(by_day: Mapping[Any, list[tuple[str, float]]], *, n_boot: int, seed: int) -> tuple[float, float]:
    days = list(by_day)
    if len(days) < 20:
        return (math.nan, math.nan)
    st, nt, sn, nn = (np.zeros(len(days)) for _ in range(4))
    for k, d in enumerate(days):
        for verdict, r in by_day[d]:
            if verdict == TRADE:
                st[k] += r
                nt[k] += 1
            else:
                sn[k] += r
                nn[k] += 1
    pick = np.random.default_rng(seed).integers(0, len(days), size=(n_boot, len(days)))
    a, b = nt[pick].sum(axis=1), nn[pick].sum(axis=1)
    ok = (a > 0) & (b > 0)
    if ok.sum() < n_boot // 2:
        return (math.nan, math.nan)
    g = st[pick].sum(axis=1)[ok] / a[ok] - sn[pick].sum(axis=1)[ok] / b[ok]
    lo, hi = np.quantile(g, [0.025, 0.975])
    return float(lo), float(hi)


def evaluate(decisions: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]], *, n_boot: int = 2000,
             seed: int = 17) -> dict[str, Any]:
    plan = RULES_V1["evaluation"]
    out_by_id = {o["signal_id"]: o for o in outcomes}
    counted, before = [], []
    for d in decisions:
        o = out_by_id.get(d["signal_id"])
        t = datetime.fromisoformat(str(d["generated_at"]))
        row = {"signal_id": d["signal_id"], "day": t.date(), "verdict": d["verdict"], "p_star": d.get("p_star"),
               "r_net": None if o is None else o.get("r_net"), "net": None if o is None else o.get("net")}
        if t.date() < EVALUATION_FROM:
            before.append(row)
        elif d["verdict"] in (TRADE, NO_TRADE) and row["r_net"] is not None:
            counted.append(row)
    trade = [r for r in counted if r["verdict"] == TRADE]
    no = [r for r in counted if r["verdict"] == NO_TRADE]
    sessions = len({r["day"] for r in counted})
    first, final = plan["first_look"], plan["final"]
    status = NOT_YET
    if min(len(trade), len(no)) >= first["min_per_group"] and sessions >= first["min_sessions"]:
        status = FIRST_LOOK
    if min(len(trade), len(no)) >= final["min_per_group"] and sessions >= final["min_sessions"]:
        status = FINAL
    rep: dict[str, Any] = {"rules_version": RULES_V1["version"], "evaluation_from": EVALUATION_FROM.isoformat(),
                           "status": status, "counted": len(counted), "trade": len(trade), "no_trade": len(no),
                           "sessions": sessions, "needed": {"first_look": first, "final": final},
                           "before_registration": before}
    if trade and no:
        gain = float(np.mean([r["r_net"] for r in trade]) - np.mean([r["r_net"] for r in no]))
        by_day: dict[Any, list[tuple[str, float]]] = defaultdict(list)
        for r in counted:
            by_day[r["day"]].append((r["verdict"], float(r["r_net"])))
        lo, hi = _ci(by_day, n_boot=n_boot, seed=seed)
        total_trade = sum(float(r["net"] or 0) for r in trade)
        total_all = sum(float(r["net"] or 0) for r in counted)
        rep.update({"mean_r_trade": round(float(np.mean([r["r_net"] for r in trade])), 3),
                    "mean_r_no_trade": round(float(np.mean([r["r_net"] for r in no])), 3),
                    "primary_metric": round(gain, 3),
                    "ci95": [None if math.isnan(lo) else round(lo, 3), None if math.isnan(hi) else round(hi, 3)],
                    "total_net_trade": round(total_trade, 2), "total_net_all": round(total_all, 2)})
        if status != NOT_YET:
            rep["passes"] = bool(math.isfinite(lo) and lo > 0 and total_trade >= total_all)
    return rep


def render_text(rep: Mapping[str, Any]) -> list[str]:
    lines = [f"Shadow decision engine {rep['rules_version']}: pre-registered evaluation from {rep['evaluation_from']}",
             f"  status {rep['status']}: {rep['counted']} counted signals ({rep['trade']} TRADE, {rep['no_trade']} "
             f"NO_TRADE) on {rep['sessions']} sessions; first look needs {rep['needed']['first_look']['min_per_group']}"
             f" per group and {rep['needed']['first_look']['min_sessions']} sessions, the final verdict "
             f"{rep['needed']['final']['min_per_group']} per group"]
    if "primary_metric" in rep:
        lines.append(f"  mean R TRADE {rep['mean_r_trade']} vs NO_TRADE {rep['mean_r_no_trade']}: difference "
                     f"{rep['primary_metric']} (95 % CI {rep['ci95']}); total net TRADE {rep['total_net_trade']} "
                     f"vs all {rep['total_net_all']}" + (f"; PASSES: {rep['passes']}" if "passes" in rep else
                                                  " (not evaluable yet: no conclusion)"))
    if rep["before_registration"]:
        lines.append(f"  {len(rep['before_registration'])} signal(s) before registration (shown, never counted):")
        for r in rep["before_registration"]:
            lines.append(f"    {r['day']} {r['signal_id']}: {r['verdict']} (p* {r['p_star']}), counterfactual R "
                         f"{r['r_net']}")
    return lines


__all__ = ["FINAL", "FIRST_LOOK", "NOT_YET", "evaluate", "render_text"]
