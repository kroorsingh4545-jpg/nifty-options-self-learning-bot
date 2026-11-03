"""Position sizing: fixed rupee risk, premium, stop distance, available margin.

For a long option position the loss if the stop is hit is::

    quantity * (entry - stop + exit_slippage) + round-trip charges

The sizer picks the largest whole number of lots for which that loss stays
within the risk budget AND the premium outlay fits the available margin.
ML/RL may only shrink size via ``size_multiplier`` in (0, 1].
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

# (quantity, entry_price, exit_price) -> total round-trip charges in rupees
CostFn = Callable[[int, float, float], float]


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    lots: int
    risk_per_unit: float
    risk_amount: float  # worst-case loss at the stop, incl. slippage + charges
    capital_required: float
    limited_by: str  # "risk" | "margin" | "freeze" | "max_lots"
    reason: str | None = None  # set when quantity == 0

    @property
    def ok(self) -> bool:
        return self.quantity > 0


def size_position(
    *,
    entry_price: float,
    stop_price: float,
    lot_size: int,
    risk_budget: float,
    available_margin: float,
    size_multiplier: float = 1.0,
    exit_slippage: float = 0.0,
    freeze_quantity: int | None = None,
    max_lots: int | None = None,
    margin_buffer: float = 0.02,
    cost_fn: CostFn | None = None,
) -> SizingResult:
    if entry_price <= 0 or lot_size <= 0:
        raise ValueError("entry_price and lot_size must be positive")
    if not 0 < size_multiplier <= 1.0:
        raise ValueError("size_multiplier must be in (0, 1]; it can only reduce size")
    risk_per_unit = entry_price - stop_price + max(0.0, exit_slippage)
    if stop_price <= 0 or stop_price >= entry_price:
        return SizingResult(0, 0, max(risk_per_unit, 0.0), 0.0, 0.0, "risk", "stop must be below entry and above 0")
    budget = max(0.0, risk_budget) * size_multiplier

    lots_by_risk = math.floor(budget / (risk_per_unit * lot_size) + 1e-9)
    lots_by_margin = math.floor(max(0.0, available_margin) / (entry_price * lot_size * (1 + margin_buffer)) + 1e-9)
    caps = {"risk": lots_by_risk, "margin": lots_by_margin}
    if freeze_quantity:
        caps["freeze"] = freeze_quantity // lot_size
    if max_lots is not None:
        caps["max_lots"] = max_lots
    limited_by = min(caps, key=lambda k: caps[k])
    lots = max(0, caps[limited_by])

    # Charges also count against the risk budget: step down until it fits.
    while lots > 0:
        qty = lots * lot_size
        charges = cost_fn(qty, entry_price, stop_price) if cost_fn else 0.0
        risk_amount = qty * risk_per_unit + charges
        if risk_amount <= budget + 1e-9:
            return SizingResult(qty, lots, risk_per_unit, round(risk_amount, 2), round(qty * entry_price, 2),
                                limited_by)
        lots -= 1
        limited_by = "risk"

    reason = {
        "margin": "insufficient margin for one lot",
        "freeze": "freeze quantity below one lot",
        "max_lots": "max_lots is zero",
    }.get(limited_by, f"risk budget {budget:.2f} too small for one lot at {risk_per_unit:.2f}/unit")
    return SizingResult(0, 0, risk_per_unit, 0.0, 0.0, limited_by, reason)
