"""Session data for ORB study E1 and the step-1 data check (pure).

A session is held as arrays over the 375 one-minute bars 09:15 .. 15:29
(index ``m`` = bar START minus 09:15, so bar ``m`` closes at 09:15 + m + 1);
a missing bar is NaN. ``include`` applies the pre-registered rule: first bar
09:15, last bar 15:29, at least ``min_bars`` bars.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

SESSION_OPEN = time(9, 15)
BARS = 375  # 09:15 .. 15:29
LAST_BAR = BARS - 1  # the 15:29 bar
SQUARE_OFF_BAR = 354  # the 15:09 bar: its close is the price at 15:10


def bar_index(t: time) -> int:
    """Bar start time -> index (may fall outside 0..374)."""
    return (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)


def bar_start(m: int) -> time:
    return (datetime.combine(date(2000, 1, 1), SESSION_OPEN) + timedelta(minutes=m)).time()


def bar_close(m: int) -> time:
    """The time at which bar ``m`` closes (its end)."""
    return bar_start(m + 1)


@dataclass
class Session:
    day: date
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    vix: np.ndarray  # India VIX close per bar (NaN where missing)
    vix_prev_close: float | None = None  # previous session's daily VIX close (fallback)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def n_bars(self) -> int:
        return int(np.isfinite(self.c).sum())

    def present(self, m: int) -> bool:
        return 0 <= m < BARS and bool(np.isfinite(self.c[m]))

    def vix_at(self, m: int) -> float | None:
        """VIX at bar ``m`` (the latest earlier minute of the day if missing, else the previous close)."""
        seg = self.vix[:m + 1]
        ok = np.flatnonzero(np.isfinite(seg))
        if ok.size:
            return float(seg[ok[-1]])
        return self.vix_prev_close


def make_session(day: date, bars: Mapping[time, tuple[float, float, float, float]],
                 vix: Mapping[time, float] | None = None, vix_prev_close: float | None = None) -> Session:
    """``bars``: {bar START: (o, h, l, c)}; ``vix``: {bar START: close}. Bars outside 09:15-15:29 are ignored
    here (``data_check`` counts them)."""
    arr = np.full((4, BARS), np.nan)
    for t, b in bars.items():
        m = bar_index(t)
        if 0 <= m < BARS:
            arr[:, m] = b
    v = np.full(BARS, np.nan)
    for t, x in (vix or {}).items():
        m = bar_index(t)
        if 0 <= m < BARS and x and x > 0:
            v[m] = x
    return Session(day, arr[0], arr[1], arr[2], arr[3], v, vix_prev_close)


def exclusion_reason(bars: Mapping[time, Any], *, min_bars: int) -> str | None:
    """Why a session is excluded (None = used)."""
    if not bars:
        return "no bars"
    starts = sorted(bars)
    inside = [t for t in starts if 0 <= bar_index(t) < BARS]
    if starts[0] != SESSION_OPEN:
        return f"first bar {starts[0].strftime('%H:%M')} (not 09:15)"
    if starts[-1] != bar_start(LAST_BAR):
        return f"last bar {starts[-1].strftime('%H:%M')} (not 15:29)"
    if len(inside) < min_bars:
        return f"{len(inside)} of {BARS} bars (< {min_bars})"
    return None


def data_check(day_bars: Mapping[date, Mapping[time, Any]], vix_bars: Mapping[date, Mapping[time, Any]], *,
               min_bars: int, weekday_holidays: Iterable[date] = ()) -> dict[str, Any]:
    """Step 1: range, sessions, exclusions, missing minutes (NIFTY and VIX) of the used sessions."""
    days = sorted(day_bars)
    excluded: list[dict[str, str]] = []
    used: list[date] = []
    for d in days:
        why = exclusion_reason(day_bars[d], min_bars=min_bars)
        if why is None:
            used.append(d)
        else:
            excluded.append({"day": d.isoformat(), "weekday": d.strftime("%a"), "reason": why})
    missing = {d: BARS - sum(1 for t in day_bars[d] if 0 <= bar_index(t) < BARS) for d in used}
    vix_missing = {d: BARS - sum(1 for t in vix_bars.get(d, {}) if 0 <= bar_index(t) < BARS) for d in used}
    by_year: dict[str, dict[str, int]] = {}
    for d in used:
        y = by_year.setdefault(str(d.year), {"sessions": 0, "missing_minutes": 0, "sessions_with_missing": 0})
        y["sessions"] += 1
        y["missing_minutes"] += missing[d]
        y["sessions_with_missing"] += int(missing[d] > 0)
    weekend_used = [d.isoformat() for d in used if d.weekday() >= 5]
    hol = sorted(weekday_holidays)
    return {
        "first_session": days[0].isoformat() if days else None,
        "last_session": days[-1].isoformat() if days else None,
        "sessions_in_store": len(days), "sessions_used": len(used),
        "first_used": used[0].isoformat() if used else None, "last_used": used[-1].isoformat() if used else None,
        "excluded": excluded,
        "weekend_sessions_used": weekend_used,
        "missing_minutes_total": int(sum(missing.values())),
        "sessions_with_missing_minutes": int(sum(1 for v in missing.values() if v > 0)),
        "worst_sessions": [{"day": d.isoformat(), "missing": int(v)} for d, v in
                           sorted(missing.items(), key=lambda kv: -kv[1])[:5] if v > 0],
        "vix_missing_minutes_total": int(sum(vix_missing.values())),
        "vix_sessions_with_missing": int(sum(1 for v in vix_missing.values() if v > 0)),
        "vix_sessions_without_any": [d.isoformat() for d, v in vix_missing.items() if v >= BARS],
        "by_year": by_year,
        "weekday_holidays": {"count": len(hol), "by_year": {str(y): sum(1 for d in hol if d.year == y)
                                                            for y in sorted({d.year for d in hol})}},
    }


def finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


__all__ = ["BARS", "LAST_BAR", "SESSION_OPEN", "SQUARE_OFF_BAR", "Session", "bar_close", "bar_index", "bar_start",
           "data_check", "exclusion_reason", "finite", "make_session"]
