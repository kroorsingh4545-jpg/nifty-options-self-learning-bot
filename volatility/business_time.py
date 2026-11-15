"""How much of an option's implied variance falls inside an intraday window
(pure; no I/O).

An annualised IV (or India VIX) is quoted on CALENDAR time: the total implied
variance to expiry is ``V = iv**2 x calendar_years``. But NIFTY only moves while
it trades, plus a jump at each open. Spreading ``V`` evenly over calendar
minutes (as if markets traded around the clock) badly understates how much an
intraday hold should move. Three conventions, all returning the variance of the
log return over a window of ``minutes`` TRADING minutes:

* ``calendar``: ``iv**2 x minutes / 525,600`` (naive; every minute of the year counts)
* ``trading``: ``iv**2 x minutes / (252 x 375)`` (all variance in trading minutes)
* ``business``: ``V`` split between trading minutes and the overnight gaps to
  expiry. A share ``overnight_share`` of each session's variance happens in the
  gap before it (R2 measured about 35 % for NIFTY); the rest is spread evenly
  over its 375 trading minutes. Gaps can carry weights (a weekend is not three
  normal nights; R2 measured about 1.9).

Matched horizons: compare an implied variance for a window with the REALISED
variance of the same window (a sum of squared log returns), so no
annualisation enters the comparison at all. ``annualise_*`` exist only to
display a variance as a familiar volatility number, with its clock named.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

TRADING_MINUTES_PER_SESSION = 375  # 09:15-15:30 IST
TRADING_SESSIONS_PER_YEAR = 252
CALENDAR_MINUTES_PER_YEAR = 365 * 24 * 60
CONVENTIONS = ("calendar", "trading", "business")


@dataclass(frozen=True)
class ExpiryClock:
    """What is left until expiry, in the units the business-time model needs."""

    calendar_years: float  # discounting and the IV quote's own clock
    trading_minutes: float  # intraday minutes left: today's remainder + every later session to the expiry close
    gap_weight: float  # sum of the overnight-gap weights before expiry (1 = a normal night)

    def __post_init__(self) -> None:
        if self.calendar_years <= 0 or self.trading_minutes < 0 or self.gap_weight < 0:
            raise ValueError("calendar_years > 0, trading_minutes >= 0 and gap_weight >= 0 required")


def clock_from_sessions(calendar_years: float, minutes_left_today: float, later_session_gap_weights: Sequence[float],
                        ) -> ExpiryClock:
    """``later_session_gap_weights``: one weight per trading session after today
    up to and including the expiry session (the weight of the gap before it)."""
    n = len(later_session_gap_weights)
    return ExpiryClock(calendar_years, minutes_left_today + n * TRADING_MINUTES_PER_SESSION,
                       float(sum(later_session_gap_weights)))


def vix_clock(*, horizon_days: float = 30.0, gap_weight_per_session: float = 1.0) -> ExpiryClock:
    """India VIX: a 30-calendar-day implied variance; on average
    ``30 x 252 / 365`` sessions (and as many gaps) fall in 30 calendar days."""
    sessions = horizon_days * TRADING_SESSIONS_PER_YEAR / 365.0
    return ExpiryClock(horizon_days / 365.0, sessions * TRADING_MINUTES_PER_SESSION, sessions * gap_weight_per_session)


def total_implied_variance(iv: float, clock: ExpiryClock) -> float:
    if iv < 0:
        raise ValueError("iv must be >= 0 (decimal)")
    return iv * iv * clock.calendar_years


def business_minute_variance(iv: float, clock: ExpiryClock, overnight_share: float) -> float:
    """Implied variance of ONE trading minute under the business-time model:
    ``V = u x [(1 - w) x M / 375 + w x G]`` (``u`` per-session variance,
    ``M`` trading minutes left, ``G`` gap weights left); a minute gets
    ``u (1 - w) / 375``."""
    if not 0.0 <= overnight_share < 1.0:
        raise ValueError("overnight_share must be in [0, 1)")
    w = overnight_share
    units = (1.0 - w) * clock.trading_minutes / TRADING_MINUTES_PER_SESSION + w * clock.gap_weight
    if units <= 0:
        return math.nan
    u = total_implied_variance(iv, clock) / units
    return u * (1.0 - w) / TRADING_MINUTES_PER_SESSION


def window_variance(iv: float, minutes: float, convention: str, *, clock: ExpiryClock | None = None,
                    overnight_share: float = 0.0) -> float:
    """Implied variance of the log return over ``minutes`` trading minutes."""
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    if convention == "calendar":
        return iv * iv * minutes / CALENDAR_MINUTES_PER_YEAR
    if convention == "trading":
        return iv * iv * minutes / (TRADING_SESSIONS_PER_YEAR * TRADING_MINUTES_PER_SESSION)
    if convention == "business":
        if clock is None:
            raise ValueError("the business convention needs the expiry clock")
        return business_minute_variance(iv, clock, overnight_share) * minutes
    raise ValueError(f"unknown convention {convention!r}; use one of {CONVENTIONS}")


def remaining_after(clock: ExpiryClock, minutes: float) -> ExpiryClock:
    """The clock after ``minutes`` trading minutes have passed inside one session
    (no gap crossed): fewer trading minutes and fewer calendar minutes."""
    return ExpiryClock(max(clock.calendar_years - minutes / CALENDAR_MINUTES_PER_YEAR, 1e-12),
                       max(clock.trading_minutes - minutes, 0.0), clock.gap_weight)


def expected_move(spot: float, variance: float) -> float:
    """One standard deviation of the move over the window, in index points."""
    return spot * math.sqrt(max(variance, 0.0))


def annualise_trading(window_var: float, minutes: float) -> float:
    """A window variance shown as an annualised volatility on TRADING time."""
    return math.sqrt(window_var / minutes * TRADING_SESSIONS_PER_YEAR * TRADING_MINUTES_PER_SESSION) \
        if minutes > 0 else math.nan


def realised_window_variance(closes: Sequence[float]) -> float:
    """Sum of squared log returns of consecutive closes (the first close is the
    window start). NaN with fewer than two closes."""
    if len(closes) < 2:
        return math.nan
    return float(sum(math.log(b / a) ** 2 for a, b in zip(closes, closes[1:], strict=False)))


def overnight_share(prev_closes: Sequence[float], opens: Sequence[float], closes: Sequence[float]) -> float:
    """Share of daily variance that happens between the close and the next open:
    ``sum(ln(O/Cprev)**2) / (sum(ln(O/Cprev)**2) + sum(ln(C/O)**2))`` (the R2
    definition). NaN without data."""
    gap = sum(math.log(o / p) ** 2 for p, o in zip(prev_closes, opens, strict=True))
    day = sum(math.log(c / o) ** 2 for o, c in zip(opens, closes, strict=True))
    return gap / (gap + day) if gap + day > 0 else math.nan


__all__ = ["CALENDAR_MINUTES_PER_YEAR", "CONVENTIONS", "TRADING_MINUTES_PER_SESSION", "TRADING_SESSIONS_PER_YEAR",
           "ExpiryClock", "annualise_trading", "business_minute_variance", "clock_from_sessions", "expected_move",
           "overnight_share", "realised_window_variance", "remaining_after", "total_implied_variance", "vix_clock",
           "window_variance"]
