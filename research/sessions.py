"""Historical NSE session calendar and NIFTY weekly expiry calendar.

Trading days are OBSERVED, not typed in: a date with a NIFTY index daily
candle was a session. Inside the observed range:

* a weekday without a candle is a holiday;
* a weekend date (or a configured holiday) WITH a candle is a special
  session (Muhurat trading, a Saturday Budget session): it is reported but
  never used as an expiry or a regular trading day.

After the last observed date (the future) the calendar falls back to
"weekday and not a configured holiday".

Weekly expiries are derived from ``weekly_expiry_rules`` (weekday by period)
and moved to the previous trading day when the rule day is a holiday. Each
derived expiry says whether it was moved and whether it has been OBSERVED in
an Upstox contract list (``observed``) or is only the rule (``rule``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from research.config import ExpiryRule


@dataclass(frozen=True)
class SessionCalendar:
    trading_days: frozenset[date]
    holidays: frozenset[date]
    special_sessions: frozenset[date]
    first: date | None
    last: date | None
    configured_holidays: frozenset[date]

    @classmethod
    def from_sessions(cls, sessions: Iterable[date], *, configured_holidays: Iterable[date] = ()) -> SessionCalendar:
        seen = set(sessions)
        configured = frozenset(configured_holidays)
        special = {d for d in seen if d.weekday() >= 5 or d in configured}
        regular = seen - special
        first, last = (min(seen), max(seen)) if seen else (None, None)
        holidays: set[date] = set()
        if first is not None and last is not None:
            d = first
            while d <= last:
                if d.weekday() < 5 and d not in seen:
                    holidays.add(d)
                d += timedelta(days=1)
        return cls(frozenset(regular), frozenset(holidays), frozenset(special), first, last, configured)

    def observed(self, d: date) -> bool:
        return self.first is not None and self.last is not None and self.first <= d <= self.last

    def is_trading_day(self, d: date) -> bool:
        if self.observed(d):
            return d in self.trading_days
        return d.weekday() < 5 and d not in self.configured_holidays

    def previous_trading_day(self, d: date, *, max_back: int = 14) -> date:
        for i in range(1, max_back + 1):
            cand = d - timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        raise LookupError(f"no trading day within {max_back} days before {d}")

    def holidays_in(self, year: int) -> list[date]:
        return sorted(d for d in self.holidays if d.year == year)

    def has_configured_year(self, year: int) -> bool:
        return any(d.year == year for d in self.configured_holidays)

    def configured_mismatches(self, year: int) -> dict[str, list[date]]:
        """Configured holidays of ``year`` vs what the candles show, inside the
        observed range only. Empty when ``year`` has no configured holidays
        (nothing to compare against)."""
        if not self.has_configured_year(year):
            return {}
        configured = {d for d in self.configured_holidays if d.year == year and self.observed(d)}
        observed = {d for d in self.holidays if d.year == year}
        return {"configured_but_traded": sorted(d for d in configured if d not in observed),
                "closed_but_not_configured": sorted(observed - configured)}


@dataclass(frozen=True)
class WeeklyExpiry:
    expiry: date
    rule_date: date
    rule_weekday: int
    shifted: bool  # rule day was not a trading day
    status: str  # observed | rule


def weekly_expiries(start: date, end: date, rules: Sequence[ExpiryRule], calendar: SessionCalendar, *,
                    observed: Iterable[date] = ()) -> list[WeeklyExpiry]:
    """One expiry per calendar week whose rule day falls in [start, end]."""
    seen = set(observed)
    out: list[WeeklyExpiry] = []
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        for rule in rules[::-1]:  # the newest rule whose weekday in this week is on/after its start
            nominal = monday + timedelta(days=rule.weekday)
            if rule.effective_from <= nominal:
                break
        else:
            monday += timedelta(days=7)
            continue
        if start <= nominal <= end:
            actual = nominal if calendar.is_trading_day(nominal) else calendar.previous_trading_day(nominal)
            out.append(WeeklyExpiry(actual, nominal, rule.weekday, actual != nominal,
                                    "observed" if actual in seen else "rule"))
        monday += timedelta(days=7)
    return out


__all__ = ["SessionCalendar", "WeeklyExpiry", "weekly_expiries"]
