"""Pure volatility mathematics for the research layer.

No I/O, no database, no network, no project state: every function takes
numbers or arrays and returns numbers or arrays, so results are reproducible
and testable (enforced by ``tests/unit/test_volatility_purity.py``).

Conventions (used everywhere, documented once here):

* Returns are natural-log returns. Variances are per-period, zero-mean
  (sum of squared log returns, the standard for daily-horizon volatility).
* "Annualised" volatility from trading sessions uses ``TRADING_DAYS = 252``.
* India VIX and option IV are annualised over CALENDAR time (365 days), like
  NSE's VIX methodology (time to expiry in minutes over the minutes in a year).
* An expected move is a one-standard-deviation range under the forecast
  volatility. It is NOT a guaranteed probability: real returns have fat tails
  and the calibration module measures how often the ranges actually hold.
"""

TRADING_DAYS = 252
CALENDAR_DAYS = 365
