"""Black-Scholes pricing and implied volatility for European index options.

NIFTY index options are European, cash-settled. These helpers are pure
functions (no state, no I/O), so the ML feature pipeline, the backtester's
SYNTHETIC data generator and later the RL environment price options the
same way.
"""

from __future__ import annotations

import math

YEAR_SECONDS = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, years: float, vol: float, rate: float, call: bool) -> float:
    """Price of a European call (``call=True``) or put. At or past expiry,
    or with zero volatility, the intrinsic value."""
    if years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    if call:
        return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
    return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_volatility(price: float, spot: float, strike: float, years: float, rate: float, call: bool, *,
                       low: float = 1e-4, high: float = 5.0, tol: float = 1e-6, max_iter: int = 200) -> float | None:
    """Volatility at which Black-Scholes reproduces ``price`` (bisection; the
    price is monotonic in volatility). ``None`` when there is no solution:
    expired, non-positive inputs, or a price outside the no-arbitrage bounds
    (below intrinsic or above the upper bound)."""
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    lo_price = black_scholes(spot, strike, years, low, rate, call)
    hi_price = black_scholes(spot, strike, years, high, rate, call)
    if not lo_price - tol <= price <= hi_price + tol:
        return None
    lo, hi = low, high
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if black_scholes(spot, strike, years, mid, rate, call) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(spot: float, strike: float, years: float, vol: float, rate: float, call: bool) -> dict[str, float] | None:
    """Black-Scholes Greeks of a European option, or ``None`` when undefined
    (expired, zero volatility or non-positive inputs).

    * ``delta``: per 1 point of the underlying
    * ``gamma``: per 1 point of the underlying, per point
    * ``theta``: per calendar DAY (negative for a long option, usually)
    * ``vega``:  per 1 volatility POINT (0.01)
    """
    if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return None
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    disc = math.exp(-rate * years)
    pdf = norm_pdf(d1)
    gamma = pdf / (spot * sq)
    vega = spot * pdf * math.sqrt(years) / 100.0
    if call:
        delta = norm_cdf(d1)
        theta = -spot * pdf * vol / (2 * math.sqrt(years)) - rate * strike * disc * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1.0
        theta = -spot * pdf * vol / (2 * math.sqrt(years)) + rate * strike * disc * norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta / 365.0, "vega": vega}


__all__ = ["YEAR_SECONDS", "black_scholes", "bs_greeks", "implied_volatility", "norm_cdf", "norm_pdf"]
