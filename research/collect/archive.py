"""Copy the bot's raw 5-second quotes (option_quotes and market_quotes) into
the research database before the bot's 30-day retention deletes them.

Reads ``trading.db`` read-only. Idempotent: a row already archived is kept,
and the latest recorded day is always copied again (it may still be growing).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, datetime

from research.collect.quotes import days_to_summarize
from research.store import ArchivedQuote, ResearchStore
from research.trading_readonly import raw_quotes, recorded_days

#: trading-database table -> archive kind
TABLES = {"option_quotes": "OPT", "market_quotes": "MKT"}


def _ms(iso: object) -> int:
    return int(datetime.fromisoformat(str(iso)).timestamp() * 1000)


def _opt_int(v: object) -> int | None:
    return None if v is None else int(v)  # type: ignore[call-overload]


def _opt_float(v: object) -> float | None:
    return None if v is None else float(v)  # type: ignore[arg-type]


def to_archived(row: dict[str, object], kind: str) -> ArchivedQuote:
    return ArchivedQuote(
        instrument_key=str(row["instrument_key"]), exchange_ms=_ms(row["exchange_time"]),
        received_ms=_ms(row["received_at"]), kind=kind, ltp=float(row["ltp"]),  # type: ignore[arg-type]
        bid=_opt_float(row.get("bid")), ask=_opt_float(row.get("ask")), bid_qty=_opt_int(row.get("bid_qty")),
        ask_qty=_opt_int(row.get("ask_qty")), volume_today=_opt_int(row.get("volume_today")),
        oi=_opt_float(row.get("oi")), iv=_opt_float(row.get("iv")), delta=_opt_float(row.get("delta")),
        gamma=_opt_float(row.get("gamma")), theta=_opt_float(row.get("theta")), vega=_opt_float(row.get("vega")),
        tbq=_opt_float(row.get("tbq")), tsq=_opt_float(row.get("tsq")), ltq=_opt_int(row.get("ltq")),
        atp=_opt_float(row.get("atp")), held=int(row.get("held") or 0))  # type: ignore[call-overload]


def archive_recorded_quotes(trading: sqlite3.Connection, store: ResearchStore, *, now: Callable[[], datetime],
                            days: Iterable[date] | None = None) -> dict[tuple[date, str], int]:
    """Archive every recorded day not yet archived (plus the latest) for both
    tables, or exactly ``days``. Returns {(day, kind): rows archived for that day}."""
    out: dict[tuple[date, str], int] = {}
    chosen = sorted(set(days)) if days is not None else None
    for table, kind in TABLES.items():
        todo = chosen if chosen is not None else days_to_summarize(recorded_days(trading, table),
                                                                    store.archived_days(kind))
        for d in todo:
            rows = [to_archived(r, kind) for r in raw_quotes(trading, d, table)]
            if rows or chosen is not None:
                out[(d, kind)] = store.archive_quotes(d, kind, rows, at=now())
    return out


__all__ = ["TABLES", "archive_recorded_quotes", "to_archived"]
