"""D7: the shadow decision engine implements its pre-registered rules exactly
(SYNTHETIC inputs)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.clock import IST
from research.decision import engine as eng
from research.decision import rules as rules_mod
from research.decision.bias import VarianceBias
from research.decision.engine import (
    INSUFFICIENT,
    NO_TRADE,
    NOT_AVAILABLE,
    TRADE,
    EngineInputs,
    breakeven_hit_rate,
    decide,
)
from research.decision.evaluate import FINAL, FIRST_LOOK, NOT_YET, evaluate
from research.decision.orb_direction import orb_direction
from research.decision.shadow import decision_row
from research.regime.samples import Sample
from research.store import ResearchStore, ResearchStoreError

T = datetime(2026, 11, 20, 11, 0, tzinfo=IST)
S, DELTA, GAMMA = 25000.0, 0.5, 0.0011


def inputs(**kw: Any) -> EngineInputs:
    base: dict[str, Any] = {"signal_id": "SIG-1", "strategy_id": "orb_options", "option_type": "CE",
                            "generated_at": T, "spot": S, "delta": DELTA, "gamma": GAMMA, "decay": 0.9, "costs": 1.4,
                            "expected_move": 45.0, "vol_change": "STABLE", "bias": 0.6, "bias_source": "STATE",
                            "bias_windows": 500}
    return EngineInputs(**{**base, **kw})


# p* ------------------------------------------------------------------------------


def test_breakeven_hit_rate_makes_expected_pnl_zero() -> None:
    v = 0.6 * (45.0 / S) ** 2
    p = breakeven_hit_rate(spot=S, delta=DELTA, gamma=GAMMA, decay=0.9, costs=1.4, variance=v)
    assert p is not None
    rng = np.random.default_rng(0)
    m = rng.normal(0.0, math.sqrt(v), 2_000_000)
    right = rng.random(m.size) < p  # the direction is right with probability p*, independent of the size
    pnl = DELTA * S * np.abs(m) * np.where(right, 1, -1) + 0.5 * GAMMA * (S * m) ** 2 - 0.9 - 1.4
    assert abs(pnl.mean()) < 0.02  # rupees per unit: the breakeven is where the expectation is zero


def test_more_variance_or_less_cost_lowers_the_breakeven() -> None:
    v = (45.0 / S) ** 2
    base = breakeven_hit_rate(spot=S, delta=DELTA, gamma=GAMMA, decay=0.9, costs=1.4, variance=0.6 * v)
    more_var = breakeven_hit_rate(spot=S, delta=DELTA, gamma=GAMMA, decay=0.9, costs=1.4, variance=1.0 * v)
    cheaper = breakeven_hit_rate(spot=S, delta=DELTA, gamma=GAMMA, decay=0.9, costs=0.2, variance=0.6 * v)
    assert base is not None and more_var is not None and cheaper is not None
    assert more_var < base and cheaper < base
    assert breakeven_hit_rate(spot=S, delta=0.0, gamma=GAMMA, decay=1, costs=1, variance=v) is None


# Verdicts ------------------------------------------------------------------------


def test_trade_at_or_below_the_registered_limit_no_trade_above() -> None:
    low = decide(inputs(bias=1.2))  # realised above implied: cheap gamma
    high = decide(inputs(bias=0.3, costs=4.0))
    assert low.verdict == TRADE and low.p_star is not None and low.p_star <= 0.55
    assert high.verdict == NO_TRADE and high.p_star is not None and high.p_star > 0.55
    assert high.confidence == pytest.approx(min(1.0, (high.p_star - 0.55) / 0.10), abs=1e-3)
    assert high.rules_version == "d7.v1" and high.rules_fingerprint == rules_mod.REGISTERED_FINGERPRINT


@pytest.mark.parametrize("missing", ["spot", "delta", "gamma", "decay", "costs", "expected_move", "vol_change", "bias"])
def test_a_missing_input_is_insufficient_evidence(missing: str) -> None:
    d = decide(inputs(**{missing: None}))
    assert d.verdict == INSUFFICIENT and d.p_star is None and d.confidence == 0.0 and d.reasons


def test_the_d5_d6_slots_are_present_and_inert() -> None:
    d = decide(inputs())
    slots = {c.name: c for c in d.components if c.source in ("D5", "D6")}
    assert set(slots) == {"D5_THRUST", "D5_PAIRFLOW", "D6_CROSSFLOW"}
    assert all(c.status == NOT_AVAILABLE for c in slots.values())
    used = {c.name for c in d.components if c.status == "USED"}
    assert used == {"hold_economics", "vol_change_iv_bias"}


def test_the_engine_refuses_to_run_unregistered_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(eng.RULES_V1, "max_breakeven_hit_rate", 0.6)
    with pytest.raises(RuntimeError, match="registered"):
        decide(inputs())


# Variance bias (point in time) ----------------------------------------------------


def sample(day: date, state: str, log_ratio: float, minutes: int = 30) -> Sample:
    t0 = datetime.combine(day, time(11, 0), tzinfo=IST)
    return Sample("CHECKPOINT", day, t0, t0 + timedelta(minutes=minutes), 1, {}, {"vol_change": state},
                  {"log_rv_implied": log_ratio})


def test_bias_uses_only_earlier_sessions_and_the_same_state() -> None:
    days = [date(2026, 1, 1) + timedelta(days=k) for k in range(300)]
    s = [sample(d, "EXPANSION", math.log(0.8)) for d in days] + [sample(d, "CONTRACTION", math.log(0.5)) for d in days]
    s.append(sample(days[-1], "EXPANSION", math.log(50.0)))  # the signal's own day: must not count
    b = VarianceBias(s, horizon=30, lookback=252, min_windows=100)
    val, source, n = b(days[-1], "EXPANSION")
    assert source == "STATE" and n == 252 and val == pytest.approx(0.8)
    assert b(days[-1], "CONTRACTION")[0] == pytest.approx(0.5)
    pooled, src, _ = b(days[-1], "STABLE")  # no STABLE windows: pooled over all states
    assert src == "POOLED" and pooled == pytest.approx(math.sqrt(0.8 * 0.5))
    assert b(days[50], "EXPANSION")[1] == "POOLED"  # 50 windows in the state (< 100), 100 in total
    assert b(days[0], "EXPANSION") == (None, None, 0)
    other = VarianceBias([sample(d, "EXPANSION", 0.0, minutes=60) for d in days], horizon=30, lookback=252,
                         min_windows=100)
    assert other(days[-1], "EXPANSION")[0] is None  # other horizons never count


# Shadow rows and the pre-registered evaluation ---------------------------------------


def outcome(sid: str, day: date, r: float, net: float) -> dict[str, Any]:
    return {"signal_id": sid, "sim_version": "d2.1", "strategy_id": "orb_options", "option_type": "CE",
            "generated_at": datetime.combine(day, time(11, 0), tzinfo=IST).isoformat(), "spot": S, "r_net": r,
            "net": net}


EVIDENCE = {"delta": DELTA, "gamma": GAMMA, "decay_business": 0.9, "costs_per_unit": 1.4,
            "expected_move_business": 45.0}


def test_decision_rows_mark_signals_before_registration() -> None:
    def bias(d: date, st: str | None) -> tuple[float | None, str | None, int]:
        return (0.6, "STATE", 300)

    before = decision_row(outcome("A", date(2026, 11, 13), 0.1, 10), {"signal_id": "A", **EVIDENCE},
                          {"signal_id": "A", "vol_change": "STABLE"}, bias)
    after = decision_row(outcome("B", date(2026, 11, 16), 0.1, 10), {"signal_id": "B", **EVIDENCE},
                         {"signal_id": "B", "vol_change": "STABLE"}, bias)
    assert before["before_registration"] == 1 and after["before_registration"] == 0
    assert json.loads(str(after["components_json"]))[2]["status"] == NOT_AVAILABLE
    none = decision_row(outcome("C", date(2026, 11, 16), 0.1, 10), None, None, bias)
    assert none["verdict"] == INSUFFICIENT


def planted(n_days: int, per_day: int, trade_r: float, no_r: float, start: date = date(2026, 11, 16)) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]]]:
    rng = np.random.default_rng(5)
    dec, out = [], []
    for k in range(n_days):
        day = start + timedelta(days=k)
        for j in range(per_day):
            sid = f"S{k}-{j}"
            verdict = TRADE if j % 2 == 0 else NO_TRADE
            r = float(rng.normal(trade_r if verdict == TRADE else no_r, 0.8))
            dec.append({"signal_id": sid, "generated_at": datetime.combine(day, time(11, 0), tzinfo=IST).isoformat(),
                        "verdict": verdict, "p_star": 0.5})
            out.append({"signal_id": sid, "r_net": r, "net": r * 1000})
    return dec, out


def test_the_evaluation_waits_for_the_registered_sample_sizes() -> None:
    assert evaluate(*planted(30, 4, 0.3, -0.3))["status"] == NOT_YET  # 60 per group, 30 sessions
    rep = evaluate(*planted(70, 4, 0.3, -0.3))  # 140 per group, 70 sessions
    assert rep["status"] == FIRST_LOOK and rep["passes"] is True and rep["ci95"][0] > 0
    final = evaluate(*planted(100, 4, 0.0, 0.0))  # 200 per group, no difference
    assert final["status"] == FINAL and final["passes"] is False


def test_signals_before_registration_are_never_counted() -> None:
    dec, out = planted(70, 4, 0.3, -0.3, start=date(2026, 9, 1))
    rep = evaluate(dec, out)
    assert rep["counted"] == 0 and rep["status"] == NOT_YET and len(rep["before_registration"]) == 280


def test_insufficient_evidence_verdicts_are_not_counted() -> None:
    dec, out = planted(70, 4, 0.3, -0.3)
    for d in dec[::2]:
        d["verdict"] = INSUFFICIENT
    rep = evaluate(dec, out)
    assert rep["trade"] == 0 and rep["status"] == NOT_YET


# ORB direction and storage ----------------------------------------------------------


def orb(day: date, direction: int, fwd: float) -> Sample:
    t0 = datetime.combine(day, time(9, 50), tzinfo=IST)
    return Sample("ORB", day, t0, t0 + timedelta(minutes=30), direction, {}, {},
                  {"fwd_ret": fwd, "cont": fwd * direction / 0.001}, {"option_type": "CE" if direction > 0 else "PE"})


def test_orb_direction_counts_moves_in_the_signals_own_direction() -> None:
    s = [orb(date(2024, 1, 2), 1, 0.001), orb(date(2024, 1, 3), 1, -0.002), orb(date(2025, 1, 2), -1, -0.001),
         orb(date(2025, 1, 3), -1, 0.0005)]
    rep = orb_direction({30: s})["30"]
    assert rep["all"]["n"] == 4 and rep["all"]["own_direction"] == 2 and rep["all"]["share"] == 0.5
    assert rep["by_year"]["2024"]["own_direction"] == 1 and rep["by_direction"]["PE"]["own_direction"] == 1
    assert rep["all"]["mean_move_bps"] == pytest.approx(1e4 * (0.001 - 0.002 + 0.001 - 0.0005) / 4)
    assert rep["all"]["ci95"][0] < 0.5 < rep["all"]["ci95"][1]


def test_decisions_are_stored_and_refuse_unknown_columns(tmp_path: Path) -> None:
    store = ResearchStore.open(tmp_path / "research.db")
    row = decision_row(outcome("A", date(2026, 11, 16), 0.1, 10), {"signal_id": "A", **EVIDENCE},
                       {"signal_id": "A", "vol_change": "STABLE"}, lambda d, s: (0.6, "STATE", 300))
    assert store.upsert_decisions([row]) == 1 and store.upsert_decisions([row]) == 1
    (got,) = store.decision_rows("d2.1", "d7.v1")
    assert got["verdict"] == row["verdict"] and got["rules_fingerprint"] == rules_mod.REGISTERED_FINGERPRINT
    with pytest.raises(ResearchStoreError):
        store.upsert_decisions([{**row, "made_up": 1}])
    store.close()
