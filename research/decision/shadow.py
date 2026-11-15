"""Engine verdicts for shadow outcomes (decision-engine D7).

Joins each D2 outcome row with its D3 evidence row and D4 regime row, adds
the point-in-time variance bias, runs ``research.decision.engine.decide`` and
returns rows for the ``shadow_decisions`` table. Signals before
``EVALUATION_FROM`` are marked ``before_registration``: they are shown but never
counted in the pre-registered evaluation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from research.decision.engine import EngineInputs, decide
from research.decision.rules import EVALUATION_FROM

BiasFn = Callable[[date, str | None], tuple[float | None, str | None, int]]


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def decision_row(o: Mapping[str, Any], ev: Mapping[str, Any] | None, rg: Mapping[str, Any] | None,
                 bias: BiasFn) -> dict[str, Any]:
    t = datetime.fromisoformat(str(o["generated_at"]))
    state = None if rg is None else rg.get("vol_change")
    b, source, n = bias(t.date(), state)
    ev = ev or {}
    x = EngineInputs(signal_id=str(o["signal_id"]), strategy_id=str(o["strategy_id"]),
                     option_type=str(o["option_type"]), generated_at=t, spot=_f(o.get("spot")),
                     delta=_f(ev.get("delta")), gamma=_f(ev.get("gamma")), decay=_f(ev.get("decay_business")),
                     costs=_f(ev.get("costs_per_unit")), expected_move=_f(ev.get("expected_move_business")),
                     vol_change=None if state is None else str(state), bias=b, bias_source=source, bias_windows=n)
    d = decide(x)
    return {"signal_id": d.signal_id, "sim_version": o["sim_version"], "rules_version": d.rules_version,
            "rules_fingerprint": d.rules_fingerprint, "generated_at": t.isoformat(),
            "before_registration": int(t.date() < EVALUATION_FROM), "verdict": d.verdict,
            "confidence": d.confidence, "p_star": d.p_star, "variance_hold": d.variance_hold, "bias": b,
            "bias_source": source, "bias_windows": n,
            "components_json": json.dumps([asdict(c) for c in d.components]),
            "reasons_json": json.dumps(list(d.reasons)), "inputs_json": json.dumps(d.inputs, default=str)}


def decision_rows(outcomes: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]],
                  regimes: Sequence[Mapping[str, Any]], bias: BiasFn) -> list[dict[str, Any]]:
    ev = {e["signal_id"]: e for e in evidence}
    rg = {r["signal_id"]: r for r in regimes}
    return [decision_row(o, ev.get(o["signal_id"]), rg.get(o["signal_id"]), bias) for o in outcomes]


__all__ = ["BiasFn", "decision_row", "decision_rows"]
