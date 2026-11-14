"""Expected move from an annualised implied volatility.

``EM = S x sigma x sqrt(T)`` with ``T`` in years. Two time conventions:

* calendar: ``T = calendar_days / 365`` (India VIX and option IV are quoted
  on calendar time). Friday close -> Monday close is 3 calendar days.
* trading: ``T = sessions / 252`` (one session = 1/252 year regardless of the
  weekend).

Which one describes NIFTY better is an empirical question (the research
measures it). The k-sigma band is ``S x exp(+/- k sigma sqrt(T))`` (log-normal
so the lower bound stays positive); the move ``S x sigma x sqrt(T)`` is the
usual quoted "expected move". A band is NOT a guaranteed probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from volatility import CALENDAR_DAYS, TRADING_DAYS


def horizon_years(*, calendar_days: float | None = None, sessions: float | None = None) -> float:
    if (calendar_days is None) == (sessions is None):
        raise ValueError("give exactly one of calendar_days or sessions")
    if calendar_days is not None:
        if calendar_days < 0:
            raise ValueError("calendar_days must be >= 0")
        return calendar_days / CALENDAR_DAYS
    assert sessions is not None
    if sessions < 0:
        raise ValueError("sessions must be >= 0")
    return sessions / TRADING_DAYS


def horizon_sigma(annual_vol: float, years: float) -> float:
    """Standard deviation of the log return over ``years``."""
    if annual_vol < 0 or years < 0:
        raise ValueError("volatility and horizon must be >= 0")
    return annual_vol * math.sqrt(years)


@dataclass(frozen=True)
class ExpectedMove:
    spot: float
    annual_vol: float
    years: float
    sigma: float  # log-return standard deviation over the horizon
    move: float  # spot x sigma (points)

    def band(self, k: float = 1.0) -> tuple[float, float]:
        return self.spot * math.exp(-k * self.sigma), self.spot * math.exp(k * self.sigma)


def expected_move(spot: float, annual_vol: float, years: float) -> ExpectedMove:
    if spot <= 0:
        raise ValueError("spot must be > 0")
    s = horizon_sigma(annual_vol, years)
    return ExpectedMove(spot, annual_vol, years, s, spot * s)


#: E[max - min] of a driftless Brownian motion over a period with standard
#: deviation sigma, in units of sigma (Feller 1951): 2 * sqrt(2 / pi).
EXPECTED_RANGE_FACTOR = 2.0 * math.sqrt(2.0 / math.pi)

__all__ = ["EXPECTED_RANGE_FACTOR", "ExpectedMove", "expected_move", "horizon_sigma", "horizon_years"]
