"""The shadow decision engine (decision-engine D7), rules v1 (pure).

Implements exactly the pre-registered ``research.decision.rules.RULES_V1``
(see ``docs/decision-engine-d7-preregistration.md``): from the D3 volatility
evidence and the D4 kept hypothesis it computes the breakeven hit rate ``p*``
of the signal's direction over the hold and returns TRADE, NO_TRADE or
INSUFFICIENT_EVIDENCE, with a confidence, every input, and the reasons.

Evidence slots for D5 (Thrust, PairFlow) and D6 (CrossFlow) are part of every
decision as NOT_AVAILABLE components; in v1 they cannot change a verdict.

A verdict is a shadow record. Nothing here can place, block, size or change an
order; the RiskManager keeps final authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from research.decision.rules import REGISTERED_FINGERPRINT, RULES_V1, RULES_VERSION, fingerprint

TRADE, NO_TRADE, INSUFFICIENT = "TRADE", "NO_TRADE", "INSUFFICIENT_EVIDENCE"
USED, NOT_AVAILABLE, MISSING = "USED", "NOT_AVAILABLE", "MISSING"


@dataclass(frozen=True)
class EngineInputs:
    signal_id: str
    strategy_id: str
    option_type: str
    generated_at: datetime
    spot: float | None = None
    delta: float | None = None
    gamma: float | None = None
    decay: float | None = None  # rupees per unit over the hold, business time (D3)
    costs: float | None = None  # rupees per unit, round trip (D3)
    expected_move: float | None = None  # 1-sd move over the hold, business clock, index points (D3)
    vol_change: str | None = None  # EXPANSION | STABLE | CONTRACTION (D4)
    bias: float | None = None  # realised / VIX-implied variance in that state, trailing, point in time (D4 kept)
    bias_source: str | None = None  # STATE | POOLED
    bias_windows: int = 0


@dataclass(frozen=True)
class Component:
    name: str
    source: str
    status: str  # USED | NOT_AVAILABLE | MISSING
    detail: str
    value: float | None = None


@dataclass(frozen=True)
class Decision:
    signal_id: str
    verdict: str
    confidence: float
    p_star: float | None
    variance_hold: float | None
    components: tuple[Component, ...]
    reasons: tuple[str, ...]
    rules_version: str = RULES_VERSION
    rules_fingerprint: str = REGISTERED_FINGERPRINT
    inputs: dict[str, Any] = field(default_factory=dict)


SLOTS = tuple(Component(name, "D5" if name.startswith("D5") else "D6", NOT_AVAILABLE,
                        f"{name}: {state}; gives no evidence and cannot change a v1 verdict")
              for name, state in RULES_V1["slots"].items())

INPUT_NAMES = {"spot": "spot", "delta": "delta", "gamma": "gamma", "decay_business": "decay",
               "costs_per_unit": "costs", "expected_move_business": "expected_move", "vol_change": "vol_change"}


def breakeven_hit_rate(*, spot: float, delta: float, gamma: float, decay: float, costs: float,
                       variance: float) -> float | None:
    """``p* = 1/2 + (decay + costs - gamma S^2 v / 2) / (2 |delta| S sqrt(2 v / pi))``;
    None when the option has no delta or the variance is not positive."""
    if variance <= 0 or abs(delta) <= 0 or spot <= 0:
        return None
    mean_abs_points = spot * math.sqrt(2.0 * variance / math.pi)
    gamma_gain = 0.5 * gamma * spot * spot * variance
    return 0.5 + (decay + costs - gamma_gain) / (2.0 * abs(delta) * mean_abs_points)


def decide(x: EngineInputs) -> Decision:
    if fingerprint(RULES_V1) != REGISTERED_FINGERPRINT:  # never run unregistered rules
        raise RuntimeError("RULES_V1 does not match its registered fingerprint")
    inputs = {k: getattr(x, k) for k in ("spot", "delta", "gamma", "decay", "costs", "expected_move", "vol_change",
                                         "bias", "bias_source", "bias_windows")}
    missing = [name for name, attr in INPUT_NAMES.items() if getattr(x, attr) is None]
    d3 = Component("hold_economics", "D3", MISSING if any(n != "vol_change" for n in missing) else USED,
                   "delta, gamma, decay (business time), costs and the 1-sd move over the "
                   f"{RULES_V1['horizon_minutes']}-minute hold")
    d4 = Component("vol_change_iv_bias", "D4", USED if x.bias is not None and x.vol_change else MISSING,
                   f"realised / VIX-implied variance in the {x.vol_change or '?'} state over the trailing "
                   f"{RULES_V1['variance']['bias_lookback_sessions']} sessions ({x.bias_source or 'no'} estimate, "
                   f"{x.bias_windows} windows)", x.bias)
    comps = (d3, d4, *SLOTS)
    if missing or x.bias is None:
        why = [f"missing input: {', '.join(missing)}"] if missing else []
        if x.bias is None:
            why.append(f"no variance-bias estimate (fewer than {RULES_V1['variance']['bias_min_windows']} windows)")
        return Decision(x.signal_id, INSUFFICIENT, 0.0, None, None, comps, tuple(why), inputs=inputs)
    assert x.spot is not None and x.delta is not None and x.gamma is not None and x.decay is not None
    assert x.costs is not None and x.expected_move is not None
    v = x.bias * (x.expected_move / x.spot) ** 2
    p = breakeven_hit_rate(spot=x.spot, delta=x.delta, gamma=x.gamma, decay=x.decay, costs=x.costs, variance=v)
    if p is None:
        return Decision(x.signal_id, INSUFFICIENT, 0.0, None, v, comps, ("no delta or no variance",), inputs=inputs)
    limit = float(RULES_V1["max_breakeven_hit_rate"])
    verdict = TRADE if p <= limit else NO_TRADE
    conf = round(min(1.0, abs(p - limit) / float(RULES_V1["confidence"]["scale"])), 3)
    gamma_gain = 0.5 * x.gamma * x.spot ** 2 * v
    reasons = (f"breakeven hit rate {p:.3f} {'<=' if verdict == TRADE else '>'} {limit:.2f}: the signal's direction "
               f"must be right {100 * p:.1f} % of the time over {RULES_V1['horizon_minutes']} minutes",
               f"decay {x.decay:.2f} + costs {x.costs:.2f} vs expected gamma gain {gamma_gain:.2f} per unit "
               f"(variance bias {x.bias:.3f}, {x.vol_change})",
               "D5 / D6 slots: no evidence yet (v1)")
    return Decision(x.signal_id, verdict, conf, round(p, 4), v, comps, reasons, inputs=inputs)


__all__ = ["INSUFFICIENT", "MISSING", "NOT_AVAILABLE", "NO_TRADE", "SLOTS", "TRADE", "USED", "Component", "Decision",
           "EngineInputs", "breakeven_hit_rate", "decide"]
