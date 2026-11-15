"""D3: option maths and its units (volatility/options.py), against finite
differences, put-call parity and the bot's options/pricing.py."""

from __future__ import annotations

import math

import pytest

from options import pricing as bot
from volatility.options import (
    MINUTES_PER_CALENDAR_YEAR,
    as_decimal_iv,
    calendar_years,
    greeks,
    implied_forward,
    implied_vol,
    infer_iv_unit,
    parity_residual,
    price,
    price_var,
)

CASES = [(25000.0, 25000.0, 4 / 365, 0.13, True), (25000.0, 25200.0, 4 / 365, 0.13, True),
         (25000.0, 24800.0, 1 / 365, 0.18, False), (24500.0, 24500.0, 0.5 / 365, 0.25, False),
         (25000.0, 26000.0, 30 / 365, 0.11, True), (25000.0, 23500.0, 30 / 365, 0.16, False)]
R = 0.065


# Units -------------------------------------------------------------------------


def test_iv_units_decimal_and_percent() -> None:
    assert as_decimal_iv(0.142, "decimal") == 0.142
    assert as_decimal_iv(14.2, "percent") == pytest.approx(0.142)
    with pytest.raises(ValueError):
        as_decimal_iv(14.2, "bps")
    assert infer_iv_unit(0.141, 0.14) == "decimal"  # the WebSocket feed
    assert infer_iv_unit(14.1, 0.14) == "percent"  # the REST option chain
    assert infer_iv_unit(1.4, 0.14) is None  # neither: refuse to guess
    assert infer_iv_unit(0.0, 0.14) is None


def test_calendar_years_use_a_365_day_year() -> None:
    assert MINUTES_PER_CALENDAR_YEAR == 525_600
    assert calendar_years(1440) == pytest.approx(1 / 365)
    assert calendar_years(525_600) == 1.0


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_vega_is_per_volatility_point(s: float, k: float, t: float, v: float, call: bool) -> None:
    g = greeks(s, k, t, v, R, 0.0, call)
    assert g is not None
    fd = (price(s, k, t, v + 0.0005, R, 0, call) - price(s, k, t, v - 0.0005, R, 0, call)) / 0.001 * 0.01
    assert g.vega == pytest.approx(fd, rel=1e-4)


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_theta_is_per_calendar_day_and_minute(s: float, k: float, t: float, v: float, call: bool) -> None:
    g = greeks(s, k, t, v, R, 0.0, call)
    assert g is not None
    dt = 1e-5
    # theta = dP/dt as time passes = -dP/dT (T = time to expiry), per year; the Greek is per calendar day
    fd_year = (price(s, k, t - dt, v, R, 0, call) - price(s, k, t + dt, v, R, 0, call)) / (2 * dt)
    assert g.theta_day == pytest.approx(fd_year / 365, rel=1e-4)
    assert g.theta_minute == pytest.approx(g.theta_day / 1440)


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_delta_and_gamma_per_point(s: float, k: float, t: float, v: float, call: bool) -> None:
    g = greeks(s, k, t, v, R, 0.0, call)
    assert g is not None
    up, dn, mid = (price(s + 1, k, t, v, R, 0, call), price(s - 1, k, t, v, R, 0, call), price(s, k, t, v, R, 0, call))
    assert g.delta == pytest.approx((up - dn) / 2, rel=1e-4, abs=1e-6)
    assert g.gamma == pytest.approx(up - 2 * mid + dn, rel=1e-3, abs=1e-8)


# Parity with the bot's pricing (q = 0) -------------------------------------------


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_prices_and_greeks_match_options_pricing(s: float, k: float, t: float, v: float, call: bool) -> None:
    assert price(s, k, t, v, R, 0.0, call) == pytest.approx(bot.black_scholes(s, k, t, v, R, call), rel=1e-12)
    mine, theirs = greeks(s, k, t, v, R, 0.0, call), bot.bs_greeks(s, k, t, v, R, call)
    assert mine is not None and theirs is not None
    for name in ("delta", "gamma", "vega"):
        assert getattr(mine, name) == pytest.approx(theirs[name], rel=1e-10)
    assert mine.theta_day == pytest.approx(theirs["theta"], rel=1e-10)


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_implied_vol_round_trips(s: float, k: float, t: float, v: float, call: bool) -> None:
    p = price(s, k, t, v, R, 0.0, call)
    if p < 0.05:
        pytest.skip("too far out of the money to invert reliably")
    iv = implied_vol(p, s, k, t, R, 0.0, call)
    assert iv == pytest.approx(v, abs=1e-5)
    assert iv == pytest.approx(bot.implied_volatility(p, s, k, t, R, call), abs=1e-5)


def test_implied_vol_refuses_impossible_prices() -> None:
    assert implied_vol(0.0, 25000, 25000, 0.01, R, 0, True) is None
    assert implied_vol(30000.0, 25000, 25000, 0.01, R, 0, True) is None  # above the upper bound
    assert implied_vol(10.0, 25000, 24000, 0.01, R, 0, True) is None  # below intrinsic


# Total variance and parity ------------------------------------------------------


@pytest.mark.parametrize(("s", "k", "t", "v", "call"), CASES)
def test_total_variance_form_equals_calendar_form(s: float, k: float, t: float, v: float, call: bool) -> None:
    assert price_var(s, k, v * v * t, t, R, 0.01, call) == pytest.approx(price(s, k, t, v, R, 0.01, call), rel=1e-12)


def test_zero_variance_is_the_discounted_forward_intrinsic() -> None:
    t = 2 / 365
    fwd = 25000 * math.exp(R * t)
    assert price_var(25000, 24900, 0.0, t, R, 0, True) == pytest.approx(math.exp(-R * t) * (fwd - 24900))
    assert price_var(25000, 26000, 0.0, t, R, 0, True) == 0.0


@pytest.mark.parametrize("q", [0.0, 0.012])
@pytest.mark.parametrize(("s", "k", "t", "v", "_call"), CASES)
def test_put_call_parity_holds_for_model_prices(s: float, k: float, t: float, v: float, _call: bool, q: float) -> None:
    c, p = price(s, k, t, v, R, q, True), price(s, k, t, v, R, q, False)
    assert abs(parity_residual(c, p, s, k, t, R, q)) < 1e-8
    assert implied_forward(c, p, k, t, R) == pytest.approx(s * math.exp((R - q) * t), rel=1e-10)


def test_greeks_are_undefined_at_expiry_or_zero_vol() -> None:
    assert greeks(25000, 25000, 0.0, 0.13, R, 0, True) is None
    assert greeks(25000, 25000, 0.01, 0.0, R, 0, True) is None
    with pytest.raises(ValueError):
        price(25000, 25000, 0.01, -0.1, R, 0, True)
