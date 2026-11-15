"""D3: clock conventions for intraday implied variance (volatility/business_time.py)."""

from __future__ import annotations

import math

import pytest

from volatility.business_time import (
    CALENDAR_MINUTES_PER_YEAR,
    TRADING_MINUTES_PER_SESSION,
    TRADING_SESSIONS_PER_YEAR,
    ExpiryClock,
    annualise_trading,
    business_minute_variance,
    clock_from_sessions,
    expected_move,
    overnight_share,
    realised_window_variance,
    remaining_after,
    total_implied_variance,
    vix_clock,
    window_variance,
)

IV = 0.15


def test_the_three_clocks_differ_by_exact_factors() -> None:
    cal = window_variance(IV, 30, "calendar")
    trd = window_variance(IV, 30, "trading")
    assert cal == pytest.approx(IV * IV * 30 / 525_600)
    assert trd == pytest.approx(IV * IV * 30 / (252 * 375))
    # counting every minute of the year understates a trading-time window by 525,600 / 94,500 = 5.56x
    trading_minutes_per_year = TRADING_SESSIONS_PER_YEAR * TRADING_MINUTES_PER_SESSION
    assert trd / cal == pytest.approx(CALENDAR_MINUTES_PER_YEAR / trading_minutes_per_year)
    assert trd / cal == pytest.approx(5.5619, abs=1e-4)
    with pytest.raises(ValueError):
        window_variance(IV, 30, "business")  # needs the expiry clock
    with pytest.raises(ValueError):
        window_variance(IV, 30, "wall")


def test_business_time_spreads_exactly_the_total_implied_variance() -> None:
    c = clock_from_sessions(4.2 / 365, 200, [1.0, 1.0, 3.5, 1.0])  # 200 min today, four more sessions, a weekend
    w = 0.35
    m = business_minute_variance(IV, c, w)
    u = m * TRADING_MINUTES_PER_SESSION / (1 - w)  # per-session variance unit
    rebuilt = m * c.trading_minutes + w * u * c.gap_weight  # every trading minute + every gap
    assert rebuilt == pytest.approx(total_implied_variance(IV, c), rel=1e-12)


def test_without_overnight_variance_business_time_is_variance_per_trading_minute_left() -> None:
    c = clock_from_sessions(2 / 365, 375, [1.0])
    assert business_minute_variance(IV, c, 0.0) == pytest.approx(IV * IV * (2 / 365) / 750)


def test_the_vix_clock_without_overnight_variance_is_the_trading_convention() -> None:
    c = vix_clock()
    assert c.trading_minutes == pytest.approx(30 * 252 / 365 * 375)
    assert window_variance(IV, 30, "business", clock=c, overnight_share=0.0) == \
        pytest.approx(window_variance(IV, 30, "trading"), rel=1e-12)
    # an overnight share moves (1 - w) of each session's variance into trading minutes
    assert window_variance(IV, 30, "business", clock=c, overnight_share=0.4) == \
        pytest.approx(0.6 * window_variance(IV, 30, "trading"), rel=1e-12)


def test_more_gap_weight_leaves_less_variance_for_trading_minutes() -> None:
    a, b = clock_from_sessions(3 / 365, 100, [1.0, 1.0]), clock_from_sessions(3 / 365, 100, [1.0, 3.0])
    assert business_minute_variance(IV, b, 0.35) < business_minute_variance(IV, a, 0.35)


def test_clock_validation_and_bounds() -> None:
    with pytest.raises(ValueError):
        ExpiryClock(0.0, 10, 0)
    with pytest.raises(ValueError):
        business_minute_variance(IV, vix_clock(), 1.0)
    with pytest.raises(ValueError):
        total_implied_variance(-0.1, vix_clock())


def test_remaining_after_uses_up_trading_and_calendar_minutes() -> None:
    c = clock_from_sessions(1 / 365, 300, [])
    later = remaining_after(c, 30)
    assert later.trading_minutes == 270 and later.calendar_years == pytest.approx((1440 - 30) / 525_600)


def test_expected_move_and_display_annualisation() -> None:
    var = window_variance(IV, 30, "trading")
    assert expected_move(25000, var) == pytest.approx(25000 * math.sqrt(var))
    assert annualise_trading(var, 30) == pytest.approx(IV)
    assert math.isnan(annualise_trading(var, 0))


def test_realised_window_variance_is_a_sum_of_squared_log_returns() -> None:
    closes = [100.0, 101.0, 100.5, 102.0]
    expect = sum(math.log(b / a) ** 2 for a, b in zip(closes, closes[1:], strict=False))
    assert realised_window_variance(closes) == pytest.approx(expect)
    assert math.isnan(realised_window_variance([100.0]))


def test_overnight_share_matches_the_r2_definition() -> None:
    prev, opens, closes = [100.0, 101.0], [101.0, 100.0], [102.0, 101.0]
    gap = math.log(101 / 100) ** 2 + math.log(100 / 101) ** 2
    day = math.log(102 / 101) ** 2 + math.log(101 / 100) ** 2
    assert overnight_share(prev, opens, closes) == pytest.approx(gap / (gap + day))
    assert math.isnan(overnight_share([100.0], [100.0], [100.0]))
