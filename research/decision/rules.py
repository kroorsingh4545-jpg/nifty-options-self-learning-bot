"""Pre-registered rules of the shadow decision engine (decision-engine D7), v1.

Fixed on 2026-11-15, BEFORE the engine was built or run on any signal. The
fingerprint of ``RULES_V1`` is recorded in
``docs/decision-engine-d7-preregistration.md`` and checked by
``tests/unit/test_decision_rules_registered.py``: changing any value fails that
test, so a rule change can only happen as a NEW version with a new registration
(and the evaluation clock restarts).

What v1 decides, per strategy signal, from evidence that exists today
(D3 volatility evidence and the one D4 hypothesis that was KEPT):

* the hit rate the signal's DIRECTION must have over the hold for the trade to
  break even after decay, costs and the volatility premium (``p_star``), and
* TRADE when ``p_star <= max_breakeven_hit_rate``, NO_TRADE above it,
  INSUFFICIENT_EVIDENCE when an input is missing.

The D5 (Thrust, PairFlow) and D6 (CrossFlow) slots exist but give no evidence
in v1 and never change a verdict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

RULES_VERSION = "d7.v1"
REGISTERED_ON = date(2026, 11, 15)
#: only signals generated on or after this day count for the pre-registered evaluation
EVALUATION_FROM = date(2026, 11, 16)

RULES_V1: dict[str, Any] = {
    "version": RULES_VERSION,
    "horizon_minutes": 30,  # the hold the evidence describes (D3 evidence horizon)
    # expected P&L of a long option over the hold, per unit, when its direction is right with probability p:
    #   E = |delta| x S x sqrt(2 v / pi) x (2p - 1) + gamma x S^2 x v / 2 - decay - costs
    # breakeven: p_star = 1/2 + (decay + costs - gamma S^2 v / 2) / (2 |delta| S sqrt(2 v / pi))
    "variance": {
        "implied": "business-clock implied variance of the hold from the option's IV (D3 expected_move_business)",
        "bias": "trailing realised / VIX-implied variance ratio of 30-minute windows (D4 samples, log mean, "
                "exponentiated) in the SAME VIX-change state, from sessions before the signal day only",
        "bias_lookback_sessions": 252,
        "bias_min_windows": 100,  # fewer in the state: use all states together; fewer still: insufficient
        "kept_hypothesis": "vol_change_iv_bias",
    },
    "max_breakeven_hit_rate": 0.55,
    "confidence": {"scale": 0.10},  # confidence = min(1, |p_star - 0.55| / 0.10)
    "required_inputs": ["spot", "delta", "gamma", "decay_business", "costs_per_unit", "expected_move_business",
                        "vol_change"],
    "slots": {"D5_THRUST": "not built", "D5_PAIRFLOW": "not built", "D6_CROSSFLOW": "not built"},
    "slot_effect_in_v1": "none",
    "verdicts": ["TRADE", "NO_TRADE", "INSUFFICIENT_EVIDENCE"],
    "evaluation": {
        "from": EVALUATION_FROM.isoformat(),
        "unit": "every shadow signal with a verdict of TRADE or NO_TRADE",
        "outcome": "D2 counterfactual net R per signal (1 lot, after charges)",
        "primary": "mean net R of TRADE minus mean net R of NO_TRADE",
        "test": "day-block bootstrap 95 % interval of the primary metric excludes zero (positive)",
        "also_required": "total net of TRADE signals >= total net of all signals",
        "first_look": {"min_per_group": 100, "min_sessions": 60},
        "final": {"min_per_group": 175, "min_sessions": 60},
        "no_retuning": "any change to these rules is a new version; its evaluation starts again from zero",
    },
}


def fingerprint(rules: dict[str, Any] = RULES_V1) -> str:
    """SHA-256 of the canonical JSON of the rules."""
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


#: the fingerprint recorded in docs/decision-engine-d7-preregistration.md
REGISTERED_FINGERPRINT = "4632769f8b6b3992264f92c519cdd809ab74adba03f228887a2dacdec871c767"

__all__ = ["EVALUATION_FROM", "REGISTERED_FINGERPRINT", "REGISTERED_ON", "RULES_V1", "RULES_VERSION", "fingerprint"]
