"""Reward design (Phase 8): components, units and safety properties."""

from __future__ import annotations

import pytest

from ml.reward_design import (
    COMPONENTS,
    RewardConfigError,
    RewardWeights,
    StepOutcome,
    compute_reward,
    reward_spec,
)


def test_unshaped_reward_equals_net_pnl_in_risk_units() -> None:
    w = RewardWeights(drawdown=0, holding_per_minute=0, overtrading_per_entry=0)
    o = StepOutcome(realized_pnl=3000.0, costs=120.0, slippage=80.0)
    rb = compute_reward(o, w, risk_budget=2000.0)
    assert rb.realized_trade_return == pytest.approx(1.5)
    assert rb.total == pytest.approx(o.net_pnl / 2000.0)
    assert o.net_pnl == pytest.approx(2800.0)


def test_every_shaping_term_is_a_penalty() -> None:
    w = RewardWeights()
    base = StepOutcome(realized_pnl=1000.0)
    shaped = StepOutcome(realized_pnl=1000.0, drawdown_increase=500.0, minutes_over_hold=30, violations=1, entries=1)
    r0 = compute_reward(base, w, 2000.0).total
    r1 = compute_reward(shaped, w, 2000.0)
    assert r1.total < r0
    for name in ("drawdown_penalty", "excessive_holding_penalty", "risk_limit_violation_penalty",
                 "overtrading_penalty"):
        assert getattr(r1, name) > 0


def test_risk_violation_is_heavier_than_a_full_stop_loss() -> None:
    w = RewardWeights()
    one_r_loss = compute_reward(StepOutcome(realized_pnl=-2000.0), w, 2000.0).total
    violation = compute_reward(StepOutcome(violations=1), w, 2000.0).total
    assert violation < one_r_loss  # a violation costs more than losing the whole risk budget
    with pytest.raises(RewardConfigError):
        RewardWeights(risk_violation=0.5)


@pytest.mark.parametrize("bad", [dict(drawdown=-1), dict(holding_per_minute=float("nan")),
                                 dict(overtrading_per_entry=-0.1), dict(max_hold_minutes=0)])
def test_weights_cannot_reward_bad_behaviour(bad: dict[str, float]) -> None:
    with pytest.raises(RewardConfigError):
        RewardWeights(**bad)  # type: ignore[arg-type]


def test_negative_costs_are_refused() -> None:
    with pytest.raises(RewardConfigError):
        compute_reward(StepOutcome(costs=-1.0), RewardWeights(), 2000.0)
    with pytest.raises(RewardConfigError):
        compute_reward(StepOutcome(), RewardWeights(), 0.0)


def test_reward_spec_lists_every_component() -> None:
    spec = reward_spec(RewardWeights())
    assert spec["components"] == list(COMPONENTS)
    for c in COMPONENTS:
        assert c in spec["formula"]
