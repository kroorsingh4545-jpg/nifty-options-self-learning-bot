"""Copy the trading bot's recorded option quotes into one-minute bars before
the bot's 30-day retention deletes them. Reads ``trading.db`` read-only."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, datetime

from research.store import ResearchStore
from research.trading_readonly import quote_minute_bars, recorded_quote_days


def days_to_summarize(recorded: Iterable[date], summarized: set[date]) -> list[date]:
    """Recorded days not yet summarized, plus always the latest recorded day
    (it may still be growing)."""
    days = sorted(set(recorded))
    if not days:
        return []
    return [d for d in days if d not in summarized or d == days[-1]]


def summarize_recorded_quotes(trading: sqlite3.Connection, store: ResearchStore, *, now: Callable[[], datetime],
                              days: Iterable[date] | None = None) -> dict[date, int]:
    todo = sorted(set(days)) if days is not None else days_to_summarize(recorded_quote_days(trading),
                                                                         store.summarized_days())
    return {d: store.upsert_quote_bars(quote_minute_bars(trading, d), at=now()) for d in todo}


__all__ = ["days_to_summarize", "summarize_recorded_quotes"]
