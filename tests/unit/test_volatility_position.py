"""D3: whole-position Greeks, hold decay, breakeven and attribution (volatility/position.py)."""

from __future__ import annotations

import math

import pytest

from volatility.business_time import clock_from_sessions
from volatility.options import Greeks, calendar_years, greeks, price
from volatility.position import Leg, attribute, breakeven_move, hold_decay, net_greeks

R = 0.065
S, K, IV = 25000.0, 25000.0, 0.13


def g(strike: float = K, call: bool = True, years: float = 4 / 365) -> Greeks:
    out = greeks(S, strike, years, IV, R, 0.0, call)
    assert out is not None
    return out


def test_position_greeks_are_quantity_weighted_in_rupees() -> None:
    ce, pe = g(), g(call=False)
    pos = net_greeks([Leg(65, ce, 120.0), Leg(130, pe, 110.0)])
    assert pos.delta_rupees == pytest.approx(65 * ce.delta + 130 * pe.delta)
    assert pos.vega_rupees == pytest.approx(65 * ce.vega + 130 * pe.vega)
    assert pos.theta_rupees_day == pytest.approx(65 * ce.theta_day + 130 * pe.theta_day)
    assert pos.value == pytest.approx(65 * 120 + 130 * 110)


def test_a_long_and_a_short_of_the_same_option_cancel() -> None:
    ce = g()
    pos = net_greeks([Leg(65, ce, 120.0), Leg(-65, ce, 120.0)])
    assert (pos.delta_rupees, pos.gamma_rupees, pos.vega_rupees, pos.theta_rupees_day, pos.value) == (0, 0, 0, 0, 0)


def test_a_straddle_is_nearly_delta_neutral_and_long_vega() -> None:
    pos = net_greeks([Leg(65, g(), 0.0), Leg(65, g(call=False), 0.0)])
    assert abs(pos.delta_rupees) < 0.15 * 65 and pos.vega_rupees > 0 and pos.theta_rupees_day < 0


def test_hold_decay_in_business_time_exceeds_calendar_theta_intraday() -> None:
    clock = clock_from_sessions(calendar_years(4 * 1440 + 300), 300, [1.0, 1.0, 3.5, 1.0])
    biz = hold_decay(S, K, IV, clock, 30, R, 0.0, True, overnight_share=0.35)
    cal = -g(years=clock.calendar_years).theta_minute * 30
    assert biz > 0 and cal > 0 and biz > 1.5 * cal  # decay happens while the market trades


def test_using_up_all_remaining_variance_leaves_the_intrinsic_value() -> None:
    clock = clock_from_sessions(calendar_years(300), 300, [])  # expiry today, 300 minutes left, no gaps
    full = hold_decay(S, 24900.0, IV, clock, 300, 0.0, 0.0, True, overnight_share=0.0)
    assert price(S, 24900.0, clock.calendar_years, IV, 0.0, 0.0, True) - full == pytest.approx(100.0, abs=1e-6)


def test_breakeven_move_solves_the_quadratic() -> None:
    d, gm, cost = 0.5, 0.0012, 6.0
    m = breakeven_move(d, gm, cost)
    assert d * m + 0.5 * gm * m * m == pytest.approx(cost)
    assert breakeven_move(-0.5, gm, cost) == pytest.approx(m)  # a put needs the same favourable move
    assert breakeven_move(0.5, 0.0, 6.0) == pytest.approx(12.0)
    assert breakeven_move(0.5, gm, 0.0) == 0.0
    assert math.isinf(breakeven_move(0.0, 0.0, 1.0))


@pytest.mark.parametrize("d_spot", [-40.0, -10.0, 15.0, 50.0])
@pytest.mark.parametrize("d_iv", [-1.0, 0.0, 0.8])
def test_attribution_explains_a_model_repricing(d_spot: float, d_iv: float) -> None:
    years, minutes = 4 / 365, 20
    clock = clock_from_sessions(years, 300, [1.0, 1.0, 1.0, 1.0])
    gr = g(years=years)
    decay = hold_decay(S, K, IV, clock, minutes, R, 0.0, True, overnight_share=0.0)
    after = years - calendar_years(minutes)
    # the hold's variance at the entry IV, plus the IV move applied to what is left
    from volatility.business_time import business_minute_variance, total_implied_variance
    from volatility.options import price_var
    v_left = total_implied_variance(IV, clock) - business_minute_variance(IV, clock, 0.0) * minutes
    v_new = v_left * ((IV + d_iv / 100) / IV) ** 2
    actual = price_var(S + d_spot, K, v_new, after, R, 0.0, True) - price(S, K, years, IV, R, 0.0, True)
    att = attribute(gr, d_spot=d_spot, d_iv_points=d_iv, decay=decay, actual=actual)
    assert att.explained == pytest.approx(att.delta + att.gamma + att.vega + att.theta)
    assert abs(att.residual) < 0.06 * (abs(actual) + 1.0)  # Taylor terms explain a small move
