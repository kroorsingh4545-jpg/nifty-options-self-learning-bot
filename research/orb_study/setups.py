"""Entries and spot exits of ORB study E1 (pure).

Source rules (``STUDY_E1["rules_source"]``): range = high / low of the first
``n`` one-minute bars; signals are 1-minute CLOSES strictly beyond the range;
stop = a later close beyond the OPPOSITE side, exit at that close; otherwise
exit at the close of ``exit_bar`` (15:29 for the source, 15:09 = the price at
15:10 for our square-off). An entry on or after ``exit_bar`` is not taken.

Our ORB entries come from ``research.regime.orb.orb_signals`` (the replica
parity-tested against the bot's strategy); their exits on premium live in
``research.orb_study.options``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

import numpy as np

from research.orb_study.data import LAST_BAR, Session, bar_index, bar_start
from research.regime.orb import OrbRule, orb_signals

BREAKOUT, RETEST, REVERSAL, OURS = "BREAKOUT", "RETEST", "REVERSAL", "OURS"
SETUPS = (BREAKOUT, RETEST, REVERSAL)
STOP, TARGET, TIME = "STOP", "TARGET", "TIME"


@dataclass(frozen=True)
class Entry:
    setup: str
    range_minutes: int
    day: Any  # date
    m: int  # entry bar (entry at its close)
    direction: int  # +1 long / bullish, -1 short / bearish
    spot: float
    hi: float
    lo: float
    day_open: float
    first_break_m: int | None = None

    @property
    def stop_side(self) -> float:
        """The range side whose close-beyond stops the trade."""
        return self.lo if self.direction > 0 else self.hi

    @property
    def risk_points(self) -> float:
        """R in spot points: entry to the stop side."""
        return abs(self.spot - self.stop_side)

    @property
    def range_pct_of_open(self) -> float:
        return 100.0 * (self.hi - self.lo) / self.day_open


@dataclass(frozen=True)
class SpotExit:
    m: int
    spot: float
    reason: str  # STOP | TARGET | TIME

    def points(self, e: Entry) -> float:
        return e.direction * (self.spot - e.spot)


def opening_range(s: Session, n: int) -> tuple[float, float] | None:
    """(high, low) of bars 0 .. n-1; None when a minute is missing."""
    if not np.isfinite(s.c[:n]).all():
        return None
    return float(np.max(s.h[:n])), float(np.min(s.lo[:n]))


def _side(close: float, hi: float, lo: float) -> int:
    return 1 if close > hi else (-1 if close < lo else 0)


def source_entries(s: Session, n: int, *, exit_bar: int = LAST_BAR) -> dict[str, Entry | None]:
    """The day's BREAKOUT, RETEST and REVERSAL entries for range length ``n``
    (each None when it did not occur)."""
    out: dict[str, Entry | None] = dict.fromkeys(SETUPS)
    rng = opening_range(s, n)
    if rng is None:
        return out
    hi, lo = rng
    day_open = float(s.o[0])
    first: tuple[int, int] | None = None
    inside_after = False
    for m in range(n, exit_bar):  # an entry on the exit bar is not taken
        c = s.c[m]
        if not np.isfinite(c):
            continue
        side = _side(float(c), hi, lo)
        if first is None:
            if side:
                first = (m, side)
                out[BREAKOUT] = Entry(BREAKOUT, n, s.day, m, side, float(c), hi, lo, day_open, m)
            continue
        fm, fd = first
        if side == 0:
            inside_after = True
        if out[REVERSAL] is None and side == -fd:
            out[REVERSAL] = Entry(REVERSAL, n, s.day, m, -fd, float(c), hi, lo, day_open, fm)
        if out[RETEST] is None and inside_after and side == fd:
            out[RETEST] = Entry(RETEST, n, s.day, m, fd, float(c), hi, lo, day_open, fm)
        if out[RETEST] is not None and out[REVERSAL] is not None:
            break
    return out


def spot_exit(s: Session, e: Entry, *, exit_bar: int = LAST_BAR, target_r: float | None = None) -> SpotExit | None:
    """Exit by the source rule: the first later close beyond the opposite side
    (exit at that close), or ``exit_bar``'s close. With ``target_r`` a spot
    target at entry + direction x target_r x R is checked first on each bar's
    favourable extreme and fills at the target level, unless the same bar also
    closes beyond the stop side (then the stop, conservatively). If the exit bar
    is missing, the last earlier bar's close is used. None when the entry is on
    or after the exit bar."""
    if e.m >= exit_bar:
        return None
    d = e.direction
    target = None if target_r is None else e.spot + d * target_r * e.risk_points
    last = e.m
    for m in range(e.m + 1, exit_bar + 1):
        c = s.c[m]
        if not np.isfinite(c):
            continue
        last = m
        stopped = float(c) < e.lo if d > 0 else float(c) > e.hi
        if stopped:
            return SpotExit(m, float(c), STOP)
        if target is not None:
            fav = float(s.h[m]) if d > 0 else float(s.lo[m])
            if (fav >= target) if d > 0 else (fav <= target):
                return SpotExit(m, target, TARGET)
        if m == exit_bar:
            return SpotExit(m, float(c), TIME)
    return SpotExit(last, float(s.c[last]), TIME)


def filtered(e: Entry | None, rule: dict[str, Any]) -> bool:
    """A source filter (``STUDY_E1["filters_source"][name]``) on an entry."""
    if e is None or e.setup != rule["setup"] or e.range_minutes != rule["range_minutes"]:
        return False
    if "min_range_pct_of_open" in rule and not e.range_pct_of_open > rule["min_range_pct_of_open"]:
        return False
    if "max_range_pct_of_open" in rule and not e.range_pct_of_open <= rule["max_range_pct_of_open"]:
        return False
    if "entry_before" in rule:
        hh, mm = (int(x) for x in str(rule["entry_before"]).split(":"))
        if not e.m + 1 < bar_index(time(hh, mm)):  # the entry bar closes before the cut-off
            return False
    limit = rule.get("max_minutes_after_first_break")
    return limit is None or (e.first_break_m is not None and e.m - e.first_break_m <= limit)


def our_entries(s: Session, rule: OrbRule, tz: Any) -> list[Entry]:
    """The bot's ORB signals on this session (every signal assumed approved)."""
    bars = {bar_start(m): (float(s.o[m]), float(s.h[m]), float(s.lo[m]), float(s.c[m]))
            for m in range(len(s.c)) if np.isfinite(s.c[m])}
    out = []
    for sig in orb_signals(s.day, bars, rule, tz):
        end = sig.candle_end
        m = bar_index((end - timedelta(minutes=1)).time())  # the last 1-minute bar of the candle
        rng = int((datetime.combine(s.day, rule.range_end) - datetime.combine(s.day, rule.range_start))
                  .total_seconds() // 60)
        out.append(Entry(OURS, rng, s.day, m, 1 if sig.option_type == "CE" else -1, float(sig.close),
                         float(sig.or_high), float(sig.or_low), float(s.o[0])))
    return out


def with_setup(e: Entry, name: str) -> Entry:
    return replace(e, setup=name)


__all__ = ["BREAKOUT", "OURS", "RETEST", "REVERSAL", "SETUPS", "STOP", "TARGET", "TIME", "Entry", "SpotExit",
           "filtered", "opening_range", "our_entries", "source_entries", "spot_exit",
           "with_setup"]
