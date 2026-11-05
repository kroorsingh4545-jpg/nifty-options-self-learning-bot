"""Reward design for the RL trade manager (Phase 8).

The reward for one environment step is, in units of the strategy's per-trade
risk budget R_budget (rupees; e.g. 2,000 -> a 2,000 rupee gain is +1.0)::

    reward = realized_trade_return          gross P&L realised this step, at unslipped prices
           - transaction_costs              brokerage, STT, exchange, SEBI, stamp duty, GST
           - slippage_penalty               adverse fill slippage (entry and exit)
           - drawdown_penalty               weight x NEW intraday equity drawdown this step
           - excessive_holding_penalty      weight x minutes held beyond ``max_hold_minutes``
           - risk_limit_violation_penalty   weight x refused / invalid actions and envelope breaches
           - overtrading_penalty            weight x entries this step

The first three terms add up to the realised NET P&L exactly (the
environment books fills at the bar price, then charges slippage and costs
separately), so an undiscounted episode return without shaping equals the
day's net P&L / R_budget. The four shaping terms are penalties only: they can
never make a losing step look profitable.

``risk_limit_violation_penalty`` is heavy by design (default 5 R_budget per
violation, more than the largest loss a single stop allows). A violation is
an action the risk envelope refused (for example ENTER with no valid setup,
or moving a stop when flat) or a forced exit because the strategy's daily
loss limit was reached while the agent held the position. The envelope still
enforces every limit; the penalty only teaches the agent not to try.

Training optimises reward, but models are judged on realised equity, trade
quality, drawdown and risk compliance (``ml.rl_agent``), never on reward
alone.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

REWARD_VERSION = "1.0.0"

COMPONENTS = ("realized_trade_return", "transaction_costs", "slippage_penalty", "drawdown_penalty",
              "excessive_holding_penalty", "risk_limit_violation_penalty", "overtrading_penalty")


class RewardConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RewardWeights:
    """Shaping weights. Costs and slippage are always charged at weight 1
    (they are real money); only the shaping terms are tunable."""

    drawdown: float = 0.5  # per R_budget of NEW intraday drawdown
    holding_per_minute: float = 0.002  # per minute beyond max_hold_minutes
    max_hold_minutes: int = 60
    risk_violation: float = 5.0  # per violation (heavy)
    overtrading_per_entry: float = 0.05  # per entry

    def __post_init__(self) -> None:
        for name in ("drawdown", "holding_per_minute", "risk_violation", "overtrading_per_entry"):
            v = getattr(self, name)
            if not math.isfinite(v) or v < 0:
                raise RewardConfigError(f"{name} must be a finite, non-negative penalty weight")
        if self.max_hold_minutes < 1:
            raise RewardConfigError("max_hold_minutes must be >= 1")
        if self.risk_violation < 1.0:
            raise RewardConfigError("risk_violation must be >= 1 R_budget: risk-limit violations are penalised heavily")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepOutcome:
    """What happened during one environment step, in rupees / minutes / counts."""

    realized_pnl: float = 0.0  # gross, at unslipped fill prices
    costs: float = 0.0
    slippage: float = 0.0
    drawdown_increase: float = 0.0  # rupees of new intraday drawdown (>= 0)
    minutes_over_hold: float = 0.0
    violations: int = 0
    entries: int = 0

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.costs - self.slippage


@dataclass(frozen=True)
class RewardBreakdown:
    realized_trade_return: float
    transaction_costs: float
    slippage_penalty: float
    drawdown_penalty: float
    excessive_holding_penalty: float
    risk_limit_violation_penalty: float
    overtrading_penalty: float

    @property
    def total(self) -> float:
        return (self.realized_trade_return - self.transaction_costs - self.slippage_penalty - self.drawdown_penalty
                - self.excessive_holding_penalty - self.risk_limit_violation_penalty - self.overtrading_penalty)

    def as_dict(self) -> dict[str, float]:
        return {**{k: float(v) for k, v in asdict(self).items()}, "total": float(self.total)}


def compute_reward(o: StepOutcome, w: RewardWeights, risk_budget: float) -> RewardBreakdown:
    """Reward components for one step (all penalties are >= 0)."""
    if not risk_budget > 0:
        raise RewardConfigError("risk_budget must be positive")
    if min(o.costs, o.slippage, o.drawdown_increase, o.minutes_over_hold) < 0 or o.violations < 0 or o.entries < 0:
        raise RewardConfigError("costs, slippage, drawdown, holding, violations and entries cannot be negative")
    return RewardBreakdown(
        realized_trade_return=o.realized_pnl / risk_budget,
        transaction_costs=o.costs / risk_budget,
        slippage_penalty=o.slippage / risk_budget,
        drawdown_penalty=w.drawdown * o.drawdown_increase / risk_budget,
        excessive_holding_penalty=w.holding_per_minute * o.minutes_over_hold,
        risk_limit_violation_penalty=w.risk_violation * o.violations,
        overtrading_penalty=w.overtrading_per_entry * o.entries,
    )


def reward_spec(w: RewardWeights) -> dict[str, Any]:
    """Machine-readable reward description (saved with every model)."""
    return {"version": REWARD_VERSION, "unit": "strategy per-trade risk budget (rupees)",
            "components": list(COMPONENTS),
            "formula": "realized_trade_return - transaction_costs - slippage_penalty - drawdown_penalty - "
                       "excessive_holding_penalty - risk_limit_violation_penalty - overtrading_penalty",
            "weights": w.as_dict()}


__all__ = ["COMPONENTS", "REWARD_VERSION", "RewardBreakdown", "RewardConfigError", "RewardWeights", "StepOutcome",
           "compute_reward", "reward_spec"]
