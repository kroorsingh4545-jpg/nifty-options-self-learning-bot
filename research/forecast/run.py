"""Build and store all forecast and outcome rows from the research database."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from research.config import ResearchConfig
from research.forecast.build import build_forecasts, build_outcomes, next_session_after
from research.forecast.panel import load_panel
from research.sessions import SessionCalendar
from research.store import ResearchStore


@dataclass
class BuildSummary:
    sessions: int = 0
    forecasts: int = 0
    outcomes: int = 0
    first: date | None = None
    last: date | None = None
    next_session: date | None = None
    next_recorded_live: bool = False
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"panel: {self.sessions} NIFTY sessions {self.first} .. {self.last}",
               f"stored: {self.forecasts} forecast rows, {self.outcomes} outcome rows"]
        if self.next_session is not None:
            kind = "recorded LIVE (before the open)" if self.next_recorded_live else "not live (after the open)"
            out.append(f"next session {self.next_session}: forecast {kind}")
        out += [f"WARNING: {w}" for w in self.warnings]
        return out


def run_build(store: ResearchStore, rcfg: ResearchConfig, *, configured_holidays: Iterable[date],
              now: datetime) -> BuildSummary:
    fc = rcfg.forecast
    nifty, vix = rcfg.instruments.nifty_index_key, rcfg.instruments.india_vix_key
    summary = BuildSummary()
    sessions = store.daily_sessions(nifty)
    if len(sessions) < fc.har_min_train:
        summary.warnings.append("not enough NIFTY daily history: run `python -m research history` first")
        return summary
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=configured_holidays)
    panel = load_panel(store, nifty, vix, special=cal.special_sessions, step=fc.intraday_step_minutes,
                       min_minutes=fc.min_intraday_minutes)
    last: date = panel["session_date"].iloc[-1]
    summary.sessions, summary.first, summary.last = len(panel), panel["session_date"].iloc[0], last
    nxt: date | None = next_session_after(cal, last)
    if nxt is not None and nxt < now.date():
        summary.warnings.append(f"history ends {last} but {nxt} was a trading day: refresh with "
                                "`python -m research history`; no next-session forecast made")
        nxt = None
    forecasts = build_forecasts(panel, fc, cal, rcfg.weekly_expiry_rules, now=now, next_session=nxt, store=store,
                                underlying=nifty)
    outcomes = build_outcomes(panel, fc, now=now)
    summary.forecasts = store.upsert_rows("vol_forecasts", forecasts)
    summary.outcomes = store.upsert_rows("vol_outcomes", outcomes)
    if nxt is not None:
        summary.next_session = nxt
        row = store.con.execute("SELECT recorded_live FROM vol_forecasts WHERE session_date = ?",
                                (nxt.isoformat(),)).fetchone()
        summary.next_recorded_live = bool(row and row[0])
    missing_vix = sum(1 for f in forecasts if f["vix_prev"] is None and f["session_date"] >= "2009-03-03")
    if missing_vix:
        summary.warnings.append(f"{missing_vix} session(s) since VIX began have no VIX forecast (missing VIX close)")
    return summary


__all__ = ["BuildSummary", "run_build"]
