"""The research database: research's only write target.

``ResearchStore.open`` refuses any path that resolves to a forbidden file
(the trading database), so research can never write trading data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.clock import now_ist
from core.models import Candle, Instrument

SCHEMA_VERSION = 2
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MINUTES_PER_DAY = 1440


class ResearchStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChainRow:
    strike: float
    option_type: str  # CE | PE
    instrument_key: str
    ltp: float | None = None
    close_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: float | None = None
    prev_oi: float | None = None
    iv_raw: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    pop: float | None = None
    strike_pcr: float | None = None


@dataclass(frozen=True)
class QuoteBar:
    instrument_key: str
    minute_start: str
    quotes: int
    open_ltp: float
    high_ltp: float
    low_ltp: float
    close_ltp: float
    last_bid: float | None
    last_ask: float | None
    last_iv: float | None
    last_oi: float | None
    last_volume_today: int | None
    held_quotes: int


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


class ResearchStore:
    def __init__(self, con: sqlite3.Connection, path: Path) -> None:
        self.con = con
        self.path = path

    @classmethod
    def open(cls, path: Path, *, forbidden: Sequence[Path] = ()) -> ResearchStore:
        """Open (and create or migrate) the research database at ``path``.
        Raises when ``path`` is one of ``forbidden`` (the trading database)."""
        for bad in forbidden:
            if _same_file(path, bad):
                raise ResearchStoreError(f"refusing to use {path} as the research database: it is the trading database")
        if path.suffix != ".db":
            raise ResearchStoreError("the research database must be a .db file")
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        store = cls(con, path)
        store._migrate()
        return store

    def _migrate(self) -> None:
        with self.con:
            self.con.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            row = self.con.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] if row else None
            if current is None:
                self.con.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                                 (SCHEMA_VERSION, now_ist().isoformat()))
            elif current > SCHEMA_VERSION:
                raise ResearchStoreError(f"research database is version {current}; this code knows {SCHEMA_VERSION}")
            elif current < SCHEMA_VERSION:  # every table is CREATE IF NOT EXISTS: the script above migrated it
                self.con.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                                 (SCHEMA_VERSION, now_ist().isoformat()))

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> ResearchStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # fetch log
    # ------------------------------------------------------------------ #
    def log_fetch(self, instrument_key: str, unit: str, interval: int, from_date: date | None, to_date: date | None,
                  rows: int, status: str, note: str | None = None, *, at: datetime | None = None) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO fetch_log(instrument_key, unit, interval, from_date, to_date, rows, status, note,"
                " fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (instrument_key, unit, interval, from_date.isoformat() if from_date else None,
                 to_date.isoformat() if to_date else None, rows, status, note, (at or now_ist()).isoformat()))

    def completed_ranges(self, instrument_key: str, unit: str, interval: int) -> set[tuple[date, date]]:
        """(from, to) ranges fetched OK at a time after ``to`` had fully ended
        (so the data in them can no longer change)."""
        rows = self.con.execute(
            "SELECT from_date, to_date, fetched_at FROM fetch_log WHERE instrument_key=? AND unit=? AND interval=?"
            " AND status='OK' AND from_date IS NOT NULL AND to_date IS NOT NULL",
            (instrument_key, unit, interval)).fetchall()
        out: set[tuple[date, date]] = set()
        for r in rows:
            to = date.fromisoformat(r["to_date"])
            if datetime.fromisoformat(r["fetched_at"]).date() > to:
                out.add((date.fromisoformat(r["from_date"]), to))
        return out

    def fetch_log(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute("SELECT * FROM fetch_log ORDER BY id")]

    # ------------------------------------------------------------------ #
    # candles
    # ------------------------------------------------------------------ #
    def upsert_daily(self, candles: Iterable[Candle], *, at: datetime | None = None) -> int:
        stamp = (at or now_ist()).isoformat()
        rows = []
        for c in candles:
            if c.timeframe_minutes != MINUTES_PER_DAY:
                raise ResearchStoreError(f"not a daily candle: {c.timeframe_minutes} minutes")
            rows.append((c.instrument_key, c.start.date().isoformat(), c.open, c.high, c.low, c.close, c.volume, c.oi,
                         int(c.complete), stamp))
        with self.con:
            self.con.executemany(
                "INSERT INTO daily_candles VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_key, session_date)"
                " DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,"
                " volume=excluded.volume, oi=excluded.oi, complete=excluded.complete, fetched_at=excluded.fetched_at",
                rows)
        return len(rows)

    def upsert_minutes(self, candles: Iterable[Candle], *, at: datetime | None = None) -> int:
        stamp = (at or now_ist()).isoformat()
        rows = [(c.instrument_key, c.timeframe_minutes, c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume,
                 c.oi, int(c.complete), stamp) for c in candles]
        if any(r[1] >= MINUTES_PER_DAY for r in rows):
            raise ResearchStoreError("daily candles belong in daily_candles")
        with self.con:
            self.con.executemany(
                "INSERT INTO minute_candles VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(instrument_key, interval_minutes, start_time) DO UPDATE SET open=excluded.open,"
                " high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, oi=excluded.oi,"
                " complete=excluded.complete, fetched_at=excluded.fetched_at", rows)
        return len(rows)

    def daily_sessions(self, instrument_key: str, *, complete_only: bool = True) -> list[date]:
        sql = "SELECT session_date FROM daily_candles WHERE instrument_key=?"
        if complete_only:
            sql += " AND complete=1"
        return [date.fromisoformat(r[0]) for r in self.con.execute(sql + " ORDER BY session_date", (instrument_key,))]

    def count(self, table: str) -> int:
        if table not in {"daily_candles", "minute_candles", "contracts", "chain_snapshots", "chain_snapshot_rows",
                         "quote_minute_bars", "fetch_log", "observations", "vol_forecasts", "vol_outcomes",
                         "reports"}:
            raise ValueError(table)
        return int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - whitelisted

    # ------------------------------------------------------------------ #
    # contracts
    # ------------------------------------------------------------------ #
    def upsert_contracts(self, instruments: Iterable[Instrument], *, at: datetime | None = None) -> int:
        stamp = (at or now_ist()).isoformat()
        rows = [(i.instrument_key, i.trading_symbol, i.underlying_key, i.instrument_type, i.strike,
                 i.expiry.isoformat() if i.expiry else None, i.lot_size,
                 None if i.weekly is None else int(i.weekly), stamp, stamp) for i in instruments]
        with self.con:
            self.con.executemany(
                "INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_key) DO UPDATE SET"
                " trading_symbol=excluded.trading_symbol, lot_size=excluded.lot_size, weekly=excluded.weekly,"
                " last_seen=excluded.last_seen", rows)
        return len(rows)

    def contracts(self) -> dict[str, dict[str, Any]]:
        return {r["instrument_key"]: dict(r) for r in self.con.execute("SELECT * FROM contracts")}

    def observed_expiries(self, underlying_key: str) -> list[date]:
        rows = self.con.execute("SELECT DISTINCT expiry FROM contracts WHERE underlying_key=? AND expiry IS NOT NULL"
                                " AND instrument_type IN ('CE','PE') ORDER BY expiry", (underlying_key,))
        return [date.fromisoformat(r[0]) for r in rows]

    # ------------------------------------------------------------------ #
    # option-chain snapshots
    # ------------------------------------------------------------------ #
    def save_chain_snapshot(self, *, taken_at: datetime, underlying_key: str, expiry: date, spot: float | None,
                            rows: Sequence[ChainRow]) -> int:
        with self.con:
            cur = self.con.execute(
                "INSERT INTO chain_snapshots(taken_at, underlying_key, expiry, spot, strikes) VALUES (?,?,?,?,?)",
                (taken_at.isoformat(), underlying_key, expiry.isoformat(), spot, len({r.strike for r in rows})))
            sid = int(cur.lastrowid or 0)
            self.con.executemany(
                "INSERT INTO chain_snapshot_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(sid, r.strike, r.option_type, r.instrument_key, r.ltp, r.close_price, r.bid, r.ask, r.bid_qty,
                  r.ask_qty, r.volume, r.oi, r.prev_oi, r.iv_raw, r.delta, r.gamma, r.theta, r.vega, r.pop,
                  r.strike_pcr) for r in rows])
        return sid

    # ------------------------------------------------------------------ #
    # recorded-quote summaries
    # ------------------------------------------------------------------ #
    def upsert_quote_bars(self, bars: Iterable[QuoteBar], *, at: datetime | None = None) -> int:
        stamp = (at or now_ist()).isoformat()
        rows = [(b.instrument_key, b.minute_start, b.quotes, b.open_ltp, b.high_ltp, b.low_ltp, b.close_ltp,
                 b.last_bid, b.last_ask, b.last_iv, b.last_oi, b.last_volume_today, b.held_quotes, stamp)
                for b in bars]
        with self.con:
            self.con.executemany(
                "INSERT INTO quote_minute_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(instrument_key, minute_start) DO UPDATE SET quotes=excluded.quotes,"
                " open_ltp=excluded.open_ltp, high_ltp=excluded.high_ltp, low_ltp=excluded.low_ltp,"
                " close_ltp=excluded.close_ltp, last_bid=excluded.last_bid, last_ask=excluded.last_ask,"
                " last_iv=excluded.last_iv, last_oi=excluded.last_oi, last_volume_today=excluded.last_volume_today,"
                " held_quotes=excluded.held_quotes, summarized_at=excluded.summarized_at", rows)
        return len(rows)

    def summarized_days(self) -> set[date]:
        rows = self.con.execute("SELECT DISTINCT substr(minute_start, 1, 10) FROM quote_minute_bars")
        return {date.fromisoformat(r[0]) for r in rows}

    # ------------------------------------------------------------------ #
    # observations
    # ------------------------------------------------------------------ #
    def record_observation(self, name: str, value: str, evidence: str, *, at: datetime | None = None) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                " evidence=excluded.evidence, observed_at=excluded.observed_at",
                (name, value, evidence, (at or now_ist()).isoformat()))

    # ------------------------------------------------------------------ #
    # forecast-vs-reality (v2)
    # ------------------------------------------------------------------ #
    def upsert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert or replace rows of ``vol_forecasts`` / ``vol_outcomes`` keyed by
        session_date. A forecast recorded live is never touched (kept as made)."""
        if table not in {"vol_forecasts", "vol_outcomes"}:
            raise ValueError(table)
        if not rows:
            return 0
        cols = list(rows[0])
        if "session_date" not in cols or any(list(r) != cols for r in rows):
            raise ResearchStoreError("rows must share the same columns, including session_date")
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "session_date")
        guard = " WHERE vol_forecasts.recorded_live = 0" if table == "vol_forecasts" else ""
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"  # noqa: S608
               f" ON CONFLICT(session_date) DO UPDATE SET {updates}{guard}")
        with self.con:
            self.con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def save_report(self, name: str, body: str, *, at: datetime | None = None) -> None:
        with self.con:
            self.con.execute("INSERT INTO reports VALUES (?,?,?)", (name, (at or now_ist()).isoformat(), body))

    def observation(self, name: str) -> str | None:
        row = self.con.execute("SELECT value FROM observations WHERE name=?", (name,)).fetchone()
        return None if row is None else str(row[0])


__all__ = ["SCHEMA_VERSION", "ChainRow", "QuoteBar", "ResearchStore", "ResearchStoreError"]
