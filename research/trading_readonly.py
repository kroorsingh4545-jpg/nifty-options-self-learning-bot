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


#: columns research reads from the bot's quote tables; any the database does not have yet
#: (a v6 database before the bot migrated it) come back as NULL
QUOTE_COLUMNS = {
    "option_quotes": ("instrument_key", "exchange_time", "received_at", "ltp", "bid", "ask", "bid_qty", "ask_qty",
                      "volume_today", "oi", "iv", "delta", "gamma", "theta", "vega", "tbq", "tsq", "ltq", "atp",
                      "held"),
    "market_quotes": ("instrument_key", "exchange_time", "received_at", "ltp", "bid", "ask", "bid_qty", "ask_qty",
                      "volume_today", "oi", "tbq", "tsq", "ltq", "atp"),
}


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    if table not in QUOTE_COLUMNS and table != "signals":
        raise ValueError(table)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return set()
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def recorded_days(con: sqlite3.Connection, table: str) -> list[date]:
    """Days on which rows of ``option_quotes`` or ``market_quotes`` were RECEIVED
    (the last-trade time of a contract that did not trade today is an earlier day)."""
    if not table_columns(con, table):
        return []
    rows = con.execute(f"SELECT DISTINCT substr(received_at, 1, 10) FROM {table} ORDER BY 1")  # noqa: S608
    return [date.fromisoformat(r[0]) for r in rows]


def signal_rows(con: sqlite3.Connection) -> list[dict[str, object]]:
    """Every row of the bot's ``signals`` table (empty before trading-db v7)."""
    if not table_columns(con, "signals"):
        return []
    return [dict(r) for r in con.execute("SELECT * FROM signals ORDER BY candle_end, signal_id")]


def closed_trades(con: sqlite3.Connection, mode: str = "PAPER") -> list[dict[str, object]]:
    """The bot's closed trades of ``mode`` (for calibrating the shadow simulator)."""
    rows = con.execute("SELECT trade_uid, strategy_id, instrument_key, quantity, created_at, entry_time, entry_price,"
                       " exit_time, exit_price, exit_reason, initial_stop_loss, target, gross_pnl, charges"
                       " FROM trades WHERE mode = ? AND status = 'CLOSED' ORDER BY created_at", (mode,))
    return [dict(r) for r in rows]


def index_close_at(con: sqlite3.Connection, instrument_key: str, at: datetime,
                   max_age: timedelta = timedelta(minutes=2)) -> tuple[float, str] | None:
    """Close of the newest complete 1-minute candle of ``instrument_key`` that
    ended at or before ``at`` (and not more than ``max_age`` before it), with
    its end time; None when there is none."""
    lo = (at - max_age).isoformat()
    row = con.execute("SELECT close, end_time FROM candles WHERE instrument_key = ? AND timeframe_minutes = 1"
                      " AND complete = 1 AND end_time <= ? AND end_time >= ? ORDER BY end_time DESC LIMIT 1",
                      (instrument_key, at.isoformat(), lo)).fetchone()
    return None if row is None else (float(row[0]), str(row[1]))


#: raw_quotes reads rows whose last trade is at most this many days before the day received
STALE_LOOKBACK_DAYS = 7


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, datetime.min.time())
    return start.strftime("%Y-%m-%dT%H:%M"), (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")


def raw_quotes(con: sqlite3.Connection, day: date, table: str) -> list[dict[str, object]]:
    """Every row of ``option_quotes`` or ``market_quotes`` RECEIVED on ``day``,
    oldest first per instrument, as plain dicts (missing columns are None).
    The exchange-time index narrows the scan: a last trade more than
    ``STALE_LOOKBACK_DAYS`` before the day it was received is not read."""
    have = table_columns(con, table)
    if not have:
        return []
    cols = QUOTE_COLUMNS[table]
    select = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
    lo, hi = _day_bounds(day)
    ex_lo, ex_hi = _day_bounds(day - timedelta(days=STALE_LOOKBACK_DAYS))[0], _day_bounds(day + timedelta(days=1))[0]
    sql = (f"SELECT {select} FROM {table} WHERE exchange_time >= ? AND exchange_time < ? "  # noqa: S608
           "AND received_at >= ? AND received_at < ? ORDER BY instrument_key, received_at, id")
    return [dict(zip(cols, r, strict=True)) for r in con.execute(sql, (ex_lo, ex_hi, lo, hi))]


def recorded_signals(con: sqlite3.Connection, day: date) -> list[dict[str, object]]:
    """The bot's recorded signals with a candle ending on ``day`` (empty before v7)."""
    if not table_columns(con, "signals"):
        return []
    lo, hi = _day_bounds(day)
    rows = con.execute("SELECT signal_id, strategy_id, candle_end, decided, approved FROM signals"
                       " WHERE candle_end >= ? AND candle_end < ? ORDER BY candle_end", (lo, hi))
    return [dict(r) for r in rows]


__all__ = ["QUOTE_COLUMNS", "STALE_LOOKBACK_DAYS", "TradingDbUnavailableError", "open_trading_db_readonly",
           "quote_minute_bars", "raw_quotes", "recorded_days", "recorded_quote_days", "recorded_signals",
           "closed_trades", "index_close_at", "signal_rows", "table_columns"]
