"""European option maths with explicit units (pure; no I/O).

NIFTY index options are European. Units used everywhere in this module:

* volatility: DECIMAL, annualised over CALENDAR time (0.14 = 14 %), like India
  VIX and option IV. The Upstox REST chain reports IV in PERCENT and the
  WebSocket feed in DECIMAL: convert with ``as_decimal_iv`` before calling.
* time: years of CALENDAR time (``calendar_years``) for discounting; the
  volatility input may instead be a TOTAL VARIANCE (sigma**2 x time, any clock)
  in the ``*_var`` functions, which lets business-time models
  (``volatility.business_time``) price intraday decay without mixing clocks.
* rates: continuously compounded, decimal (``r`` rate, ``q`` dividend yield).
* Greeks of ONE unit of the option (multiply by quantity for a position):
  ``delta`` per 1 index point; ``gamma`` per point per point; ``vega`` per ONE
  VOLATILITY POINT (0.01); ``theta_day`` per CALENDAR DAY (negative for a long
  option, usually), matching ``options/pricing.bs_greeks``.

``tests/unit/test_volatility_options.py`` checks these against finite
differences and against the bot's ``options/pricing.py`` (q = 0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CALENDAR_DAYS = 365.0
MINUTES_PER_CALENDAR_YEAR = CALENDAR_DAYS * 24 * 60


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def calendar_years(minutes: float) -> float:
    """Calendar minutes -> years (365-day year)."""
    return minutes / MINUTES_PER_CALENDAR_YEAR


def as_decimal_iv(value: float, unit: str) -> float:
    """An IV as a decimal from its source unit ('decimal' or 'percent')."""
    if unit == "decimal":
        return value
    if unit == "percent":
        return value / 100.0
    raise ValueError(f"unknown IV unit {unit!r}")


def infer_iv_unit(raw: float, solved_decimal: float) -> str | None:
    """'decimal' or 'percent' from the ratio of a raw IV to one solved from the
    same price; None when neither fits (ratio not near 1 or near 100)."""
    if raw <= 0 or solved_decimal <= 0:
        return None
    ratio = raw / solved_decimal
    if 0.5 <= ratio <= 2.0:
        return "decimal"
    if 50.0 <= ratio <= 200.0:
        return "percent"
    return None


def intrinsic(spot: float, strike: float, call: bool) -> float:
    return max(0.0, spot - strike) if call else max(0.0, strike - spot)


def price_var(spot: float, strike: float, total_var: float, years: float, r: float, q: float, call: bool) -> float:
    """Black-Scholes price with the volatility given as TOTAL VARIANCE
    (sigma**2 x time); ``years`` (calendar) discounts. At zero variance the
    discounted forward intrinsic value."""
    if spot <= 0 or strike <= 0 or years < 0 or total_var < 0:
        raise ValueError("spot, strike > 0 and years, total_var >= 0 required")
    df_r, df_q = math.exp(-r * years), math.exp(-q * years)
    fwd = spot * df_q / df_r
    if total_var == 0:
        return df_r * (max(0.0, fwd - strike) if call else max(0.0, strike - fwd))
    sd = math.sqrt(total_var)
    d1 = (math.log(fwd / strike) + 0.5 * total_var) / sd
    d2 = d1 - sd
    if call:
        return df_r * (fwd * norm_cdf(d1) - strike * norm_cdf(d2))
    return df_r * (strike * norm_cdf(-d2) - fwd * norm_cdf(-d1))


def price(spot: float, strike: float, years: float, vol: float, r: float, q: float, call: bool) -> float:
    """Classic Black-Scholes(-Merton) on calendar time."""
    if vol < 0:
        raise ValueError("vol must be >= 0")
    return price_var(spot, strike, vol * vol * years, years, r, q, call)


@dataclass(frozen=True)
class Greeks:
    """Greeks of ONE option unit (see the module doc for units)."""

    delta: float  # per 1 index point
    gamma: float  # per point per point
    vega: float  # per 1 volatility point (0.01)
    theta_day: float  # per calendar day

    @property
    def theta_minute(self) -> float:
        """Calendar-time decay per minute (theta_day / 1440)."""
        return self.theta_day / 1440.0


def greeks(spot: float, strike: float, years: float, vol: float, r: float, q: float, call: bool) -> Greeks | None:
    """Black-Scholes-Merton Greeks; None when undefined (expired or zero vol)."""
    if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return None
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    df_r, df_q = math.exp(-r * years), math.exp(-q * years)
    pdf = norm_pdf(d1)
    gamma = df_q * pdf / (spot * sq)
    vega = spot * df_q * pdf * math.sqrt(years) / 100.0
    common = -spot * df_q * pdf * vol / (2.0 * math.sqrt(years))
    if call:
        delta = df_q * norm_cdf(d1)
        theta = common - r * strike * df_r * norm_cdf(d2) + q * spot * df_q * norm_cdf(d1)
    else:
        delta = df_q * (norm_cdf(d1) - 1.0)
        theta = common + r * strike * df_r * norm_cdf(-d2) - q * spot * df_q * norm_cdf(-d1)
    return Greeks(delta, gamma, vega, theta / CALENDAR_DAYS)


def implied_vol(option_price: float, spot: float, strike: float, years: float, r: float, q: float, call: bool, *,
                low: float = 1e-4, high: float = 5.0, tol: float = 1e-7, max_iter: int = 200) -> float | None:
    """Decimal implied volatility by bisection; None outside the no-arbitrage bounds."""
    if option_price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    lo_p, hi_p = price(spot, strike, years, low, r, q, call), price(spot, strike, years, high, r, q, call)
    if not lo_p - tol <= option_price <= hi_p + tol:
        return None
    lo, hi = low, high
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if price(spot, strike, years, mid, r, q, call) < option_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def parity_residual(call_price: float, put_price: float, spot: float, strike: float, years: float, r: float,
                    q: float) -> float:
    """C - P - (S e^{-qT} - K e^{-rT}); zero for arbitrage-free European prices."""
    return call_price - put_price - (spot * math.exp(-q * years) - strike * math.exp(-r * years))


def implied_forward(call_price: float, put_price: float, strike: float, years: float, r: float) -> float:
    """The forward that makes put-call parity hold: F = K + (C - P) e^{rT}."""
    return strike + (call_price - put_price) * math.exp(r * years)


__all__ = ["CALENDAR_DAYS", "MINUTES_PER_CALENDAR_YEAR", "Greeks", "as_decimal_iv", "calendar_years", "greeks",
           "implied_forward", "implied_vol", "infer_iv_unit", "intrinsic", "norm_cdf", "norm_pdf", "parity_residual",
           "price", "price_var"]
