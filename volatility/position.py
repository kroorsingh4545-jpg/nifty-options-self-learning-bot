"""Whole-position Greeks, the decay of a hold, the move needed to break even,
and a P&L attribution (pure; no I/O).

A position is a list of legs, each a signed quantity of option UNITS (lots x
lot size; negative = short) with its per-unit Greeks (``volatility.options``
units). Position figures are in RUPEES:

* ``delta_rupees``: P&L for a 1-point NIFTY rise
* ``gamma_rupees``: change of that per further point
* ``vega_rupees``: P&L for a 1-volatility-point (0.01) IV rise
* ``theta_rupees_day``: calendar-time decay per day

Intraday decay is NOT ``theta_day x minutes / 1440``: options lose value as
their remaining IMPLIED VARIANCE is used up, and that happens in trading time
and at the opens (``volatility.business_time``). ``hold_decay`` reprices the
option with the variance of the hold removed, which keeps the clocks matched.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from volatility.business_time import ExpiryClock, business_minute_variance, total_implied_variance
from volatility.options import Greeks, calendar_years, price_var


@dataclass(frozen=True)
class Leg:
    quantity: int  # signed option units (lots x lot size); negative = short
    greeks: Greeks  # per unit
    price: float = 0.0  # per unit (premium), for the position value


@dataclass(frozen=True)
class PositionGreeks:
    delta_rupees: float
    gamma_rupees: float
    vega_rupees: float
    theta_rupees_day: float
    value: float  # sum of quantity x price (premium paid is positive for longs)


def net_greeks(legs: Sequence[Leg]) -> PositionGreeks:
    return PositionGreeks(
        delta_rupees=sum(leg.quantity * leg.greeks.delta for leg in legs),
        gamma_rupees=sum(leg.quantity * leg.greeks.gamma for leg in legs),
        vega_rupees=sum(leg.quantity * leg.greeks.vega for leg in legs),
        theta_rupees_day=sum(leg.quantity * leg.greeks.theta_day for leg in legs),
        value=sum(leg.quantity * leg.price for leg in legs))


def hold_decay(spot: float, strike: float, iv: float, clock: ExpiryClock, minutes: float, r: float, q: float,
               call: bool, *, overnight_share: float) -> float:
    """Premium lost by ONE unit over a hold of ``minutes`` trading minutes (no
    gap crossed) with spot and IV unchanged: the price at the full remaining
    implied variance minus the price with the hold's business-time variance
    used up (and the calendar minutes elapsed for discounting)."""
    v_total = total_implied_variance(iv, clock)
    v_hold = business_minute_variance(iv, clock, overnight_share) * minutes
    years = clock.calendar_years
    later = max(years - calendar_years(minutes), 1e-12)
    return (price_var(spot, strike, v_total, years, r, q, call)
            - price_var(spot, strike, max(v_total - v_hold, 0.0), later, r, q, call))


def breakeven_move(delta: float, gamma: float, cost_per_unit: float) -> float:
    """Index points the market must move IN THE POSITION'S FAVOUR for a long
    option to recover ``cost_per_unit`` (decay + charges + spread):
    ``|delta| m + gamma m**2 / 2 = cost``. Infinite when the option has no delta."""
    if cost_per_unit <= 0:
        return 0.0
    d = abs(delta)
    if gamma <= 1e-12:
        return cost_per_unit / d if d > 0 else math.inf
    return (-d + math.sqrt(d * d + 2.0 * gamma * cost_per_unit)) / gamma


@dataclass(frozen=True)
class Attribution:
    """Option P&L of one unit split by source (Taylor terms; ``residual`` is
    what they do not explain)."""

    delta: float
    gamma: float
    vega: float
    theta: float
    explained: float
    actual: float
    residual: float


def attribute(g: Greeks, *, d_spot: float, d_iv_points: float, decay: float, actual: float) -> Attribution:
    """``decay`` is the premium lost to time over the hold (``hold_decay``), a
    positive number for a long option; ``d_iv_points`` in volatility points."""
    parts = (g.delta * d_spot, 0.5 * g.gamma * d_spot * d_spot, g.vega * d_iv_points, -decay)
    explained = sum(parts)
    return Attribution(*parts, explained=explained, actual=actual, residual=actual - explained)


__all__ = ["Attribution", "Leg", "PositionGreeks", "attribute", "breakeven_move", "hold_decay", "net_greeks"]
