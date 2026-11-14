"""Read-only access to the trading database (``data/trading.db``).

The connection is opened with SQLite's ``mode=ro`` URI flag and
``PRAGMA query_only``, so no statement through it can write, even by
mistake. This is the ONLY place research opens the trading database.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from research.store.store import QuoteBar

#: option_quotes.exchange_time is IST (no daylight saving), stored with this offset
IST_SUFFIX = "+05:30"


class TradingDbUnavailableError(FileNotFoundError):
    pass


def open_trading_db_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise TradingDbUnavailableError(f"trading database not found: {path}")
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def recorded_quote_days(con: sqlite3.Connection) -> list[date]:
    rows = con.execute("SELECT DISTINCT substr(exchange_time, 1, 10) FROM option_quotes ORDER BY 1")
    return [date.fromisoformat(r[0]) for r in rows]


_BARS_SQL = """
WITH q AS (
    SELECT instrument_key AS k, substr(exchange_time, 1, 16) AS m, ltp, bid, ask, iv, oi, volume_today, held,
           ROW_NUMBER() OVER (PARTITION BY instrument_key, substr(exchange_time, 1, 16)
                              ORDER BY exchange_time, id) AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY instrument_key, substr(exchange_time, 1, 16)
                              ORDER BY exchange_time DESC, id DESC) AS rn_last
    FROM option_quotes
    WHERE exchange_time >= ? AND exchange_time < ?
)
SELECT k, m, COUNT(*) AS n,
       MAX(CASE WHEN rn_first = 1 THEN ltp END) AS o, MAX(ltp) AS h, MIN(ltp) AS l,
       MAX(CASE WHEN rn_last = 1 THEN ltp END) AS c,
       MAX(CASE WHEN rn_last = 1 THEN bid END) AS bid, MAX(CASE WHEN rn_last = 1 THEN ask END) AS ask,
       MAX(CASE WHEN rn_last = 1 THEN iv END) AS iv, MAX(CASE WHEN rn_last = 1 THEN oi END) AS oi,
       MAX(CASE WHEN rn_last = 1 THEN volume_today END) AS vol, SUM(held) AS held
FROM q GROUP BY k, m ORDER BY k, m
"""


def quote_minute_bars(con: sqlite3.Connection, day: date) -> list[QuoteBar]:
    """One bar per contract per minute of ``day`` from ``option_quotes``
    (exchange time; the last quote of the minute gives bid/ask/IV/OI)."""
    start = datetime.combine(day, datetime.min.time())
    lo, hi = start.strftime("%Y-%m-%dT%H:%M"), (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    return [QuoteBar(instrument_key=r["k"], minute_start=f"{r['m']}:00{IST_SUFFIX}", quotes=int(r["n"]),
                     open_ltp=float(r["o"]), high_ltp=float(r["h"]), low_ltp=float(r["l"]), close_ltp=float(r["c"]),
                     last_bid=r["bid"], last_ask=r["ask"], last_iv=r["iv"], last_oi=r["oi"],
                     last_volume_today=r["vol"], held_quotes=int(r["held"] or 0))
            for r in con.execute(_BARS_SQL, (lo, hi))]


__all__ = ["TradingDbUnavailableError", "open_trading_db_readonly", "quote_minute_bars", "recorded_quote_days"]
