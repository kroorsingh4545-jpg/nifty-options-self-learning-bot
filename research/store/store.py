"""The research database: research's only write target.

``ResearchStore.open`` refuses any path that resolves to a forbidden file
(the trading database), so research can never write trading data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from core.clock import IST as IST_TZ
from core.clock import now_ist
from core.models import Candle, Instrument

SCHEMA_VERSION = 7  # v3: quote_archive, archive_log (D1); v4: shadow_outcomes (D2); v5: shadow_evidence (D3);
#                    v6: shadow_regime (D4); v7: shadow_decisions (D7)

DECISION_COLUMNS = (
    "signal_id",
    "sim_version",
    "rules_version",
    "rules_fingerprint",
    "generated_at",
    "before_registration",
    "verdict",
    "confidence",
    "p_star",
    "variance_hold",
    "bias",
    "bias_source",
    "bias_windows",
    "components_json",
    "reasons_json",
    "inputs_json",
)

REGIME_COLUMNS = (
    "signal_id",
    "sim_version",
    "regime_version",
    "kept_json",
    "regime_named",
    "regime_trend",
    "regime_vol",
    "vol_change",
    "expiry_day",
    "regime_confidence",
    "regime_reasons_json",
    "pulse_label",
    "pulse_direction",
    "pulse_confidence",
    "pulse_aligned",
    "pulse_reasons_json",
    "er",
    "adx",
    "disp",
    "atrx",
    "ml_trend",
    "ml_vol",
)

EVIDENCE_COLUMNS = (
    "signal_id",
    "sim_version",
    "evidence_version",
    "overnight_share",
    "minutes_to_expiry",
    "trading_minutes_to_expiry",
    "gap_weight_to_expiry",
    "iv_feed",
    "iv_solved",
    "iv_feed_unit",
    "iv_entry",
    "delta",
    "gamma",
    "vega",
    "theta_day",
    "position_delta_rupees",
    "position_gamma_rupees",
    "position_vega_rupees",
    "position_theta_rupees_day",
    "horizon_minutes",
    "decay_business",
    "decay_calendar",
    "costs_per_unit",
    "breakeven_points",
    "expected_move_calendar",
    "expected_move_trading",
    "expected_move_business",
    "breakeven_to_move",
    "hold_minutes",
    "realised_var_hold",
    "implied_var_hold",
    "realised_to_implied",
    "iv_exit",
    "iv_change_points",
    "pnl_delta",
    "pnl_gamma",
    "pnl_vega",
    "pnl_decay",
    "pnl_explained",
    "pnl_actual_mid",
    "pnl_residual",
)
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
class ArchivedQuote:
    """One raw quote row from the bot's option_quotes (OPT) or market_quotes (MKT)."""

    instrument_key: str
    exchange_ms: int
    received_ms: int
    kind: str  # OPT | MKT
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume_today: int | None = None
    oi: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    tbq: float | None = None
    tsq: float | None = None
    ltq: int | None = None
    atp: float | None = None
    held: int = 0


ARCHIVE_COLUMNS = ("instrument_key", "exchange_ms", "received_ms", "kind", "ltp", "bid", "ask", "bid_qty", "ask_qty",
                   "volume_today", "oi", "iv", "delta", "gamma", "theta", "vega", "tbq", "tsq", "ltq", "atp", "held")


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
                         "reports", "quote_archive", "archive_log", "shadow_outcomes", "shadow_evidence",
                         "shadow_regime", "shadow_decisions"}:
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

    def archive_quotes(self, day: date, kind: str, rows: Sequence[ArchivedQuote], *,
                       at: datetime | None = None) -> int:
        """Copy raw quotes received on one session day; rows already archived
        are kept (idempotent). Returns how many rows the day now has for ``kind``."""
        if kind not in ("OPT", "MKT") or any(r.kind != kind for r in rows):
            raise ValueError("kind must be OPT or MKT and match every row")
        marks = ",".join("?" * len(ARCHIVE_COLUMNS))
        with self.con:
            self.con.executemany(
                f"INSERT OR IGNORE INTO quote_archive ({','.join(ARCHIVE_COLUMNS)}) VALUES ({marks})",  # noqa: S608
                [tuple(getattr(r, c) for c in ARCHIVE_COLUMNS) for r in rows])
            start = int(datetime.combine(day, datetime.min.time(), tzinfo=IST_TZ).timestamp() * 1000)
            n = int(self.con.execute("SELECT COUNT(*) FROM quote_archive WHERE kind=? AND received_ms>=? AND"
                                     " received_ms<?", (kind, start, start + 86_400_000)).fetchone()[0])
            self.con.execute("INSERT INTO archive_log VALUES (?,?,?,?) ON CONFLICT(session_date, kind) DO UPDATE SET"
                             " rows=excluded.rows, archived_at=excluded.archived_at",
                             (day.isoformat(), kind, n, (at or now_ist()).isoformat()))
        return n

    def archived_quotes(self, instrument_key: str, start_ms: int, end_ms: int, *, kind: str = "OPT"
                        ) -> list[sqlite3.Row]:
        """Archived quotes of one instrument received in [start_ms, end_ms], oldest first."""
        return list(self.con.execute(
            "SELECT * FROM quote_archive WHERE instrument_key=? AND kind=? AND received_ms BETWEEN ? AND ?"
            " ORDER BY received_ms, exchange_ms", (instrument_key, kind, start_ms, end_ms)))

    def last_archived_quote(self, instrument_key: str, at_ms: int, *, kind: str = "MKT",
                            max_age_ms: int = 60_000) -> sqlite3.Row | None:
        """The newest archived quote of ``instrument_key`` received at or before
        ``at_ms`` and at most ``max_age_ms`` old."""
        row: sqlite3.Row | None = self.con.execute(
            "SELECT * FROM quote_archive WHERE instrument_key=? AND kind=? AND received_ms BETWEEN ? AND ?"
            " ORDER BY received_ms DESC LIMIT 1", (instrument_key, kind, at_ms - max_age_ms, at_ms)).fetchone()
        return row

    def upsert_shadow(self, rows: Sequence[Mapping[str, Any]], *, at: datetime | None = None) -> int:
        """Insert or replace shadow outcomes (recomputed when the data or the simulator improves)."""
        if not rows:
            return 0
        stamp = (at or now_ist()).isoformat()
        cols = [*rows[0], "computed_at"]
        if any(list(r) != cols[:-1] for r in rows):
            raise ResearchStoreError("shadow rows must share the same columns")
        sql = (f"INSERT OR REPLACE INTO shadow_outcomes ({', '.join(cols)}) "  # noqa: S608 - fixed column names
               f"VALUES ({', '.join('?' * len(cols))})")
        with self.con:
            self.con.executemany(sql, [(*r.values(), stamp) for r in rows])
        return len(rows)

    def upsert_evidence(self, rows: Sequence[Mapping[str, Any]], *, at: datetime | None = None) -> int:
        """Insert or replace volatility evidence rows; missing fields are stored as NULL."""
        if not rows:
            return 0
        unknown = {k for r in rows for k in r} - set(EVIDENCE_COLUMNS)
        if unknown:
            raise ResearchStoreError(f"unknown evidence columns: {sorted(unknown)}")
        stamp = (at or now_ist()).isoformat()
        cols = [*EVIDENCE_COLUMNS, "computed_at"]
        sql = (f"INSERT OR REPLACE INTO shadow_evidence ({', '.join(cols)}) "  # noqa: S608 - fixed column names
               f"VALUES ({', '.join('?' * len(cols))})")
        with self.con:
            self.con.executemany(sql, [(*(r.get(c) for c in EVIDENCE_COLUMNS), stamp) for r in rows])
        return len(rows)

    def upsert_regime(self, rows: Sequence[Mapping[str, Any]], *, at: datetime | None = None) -> int:
        """Insert or replace regime / Pulse rows; missing fields are stored as NULL."""
        if not rows:
            return 0
        unknown = {k for r in rows for k in r} - set(REGIME_COLUMNS)
        if unknown:
            raise ResearchStoreError(f"unknown regime columns: {sorted(unknown)}")
        stamp = (at or now_ist()).isoformat()
        cols = [*REGIME_COLUMNS, "computed_at"]
        sql = (f"INSERT OR REPLACE INTO shadow_regime ({', '.join(cols)}) "  # noqa: S608 - fixed column names
               f"VALUES ({', '.join('?' * len(cols))})")
        with self.con:
            self.con.executemany(sql, [(*(r.get(c) for c in REGIME_COLUMNS), stamp) for r in rows])
        return len(rows)

    def upsert_decisions(self, rows: Sequence[Mapping[str, Any]], *, at: datetime | None = None) -> int:
        """Insert or replace engine verdicts (recomputed as evidence arrives; the rules never change)."""
        if not rows:
            return 0
        unknown = {k for r in rows for k in r} - set(DECISION_COLUMNS)
        if unknown:
            raise ResearchStoreError(f"unknown decision columns: {sorted(unknown)}")
        stamp = (at or now_ist()).isoformat()
        cols = [*DECISION_COLUMNS, "computed_at"]
        sql = (f"INSERT OR REPLACE INTO shadow_decisions ({', '.join(cols)}) "  # noqa: S608 - fixed column names
               f"VALUES ({', '.join('?' * len(cols))})")
        with self.con:
            self.con.executemany(sql, [(*(r.get(c) for c in DECISION_COLUMNS), stamp) for r in rows])
        return len(rows)

    def decision_rows(self, sim_version: str, rules_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM shadow_decisions WHERE sim_version=? AND rules_version=? ORDER BY generated_at",
            (sim_version, rules_version))]

    def regime_rows(self, sim_version: str, regime_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM shadow_regime WHERE sim_version=? AND regime_version=? ORDER BY signal_id",
            (sim_version, regime_version))]

    def minute_bars(self, instrument_key: str, day: date) -> dict[time, tuple[float, float, float, float]]:
        """{bar START time: (open, high, low, close)} of stored complete 1-minute candles of ``day``."""
        rows = self.con.execute("SELECT start_time, open, high, low, close FROM minute_candles WHERE"
                                " instrument_key=? AND interval_minutes=1 AND complete=1 AND start_time >= ? AND"
                                " start_time < ? ORDER BY start_time",
                                (instrument_key, day.isoformat(), (day + timedelta(days=1)).isoformat()))
        return {datetime.fromisoformat(r[0]).time(): (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in rows}

    def latest_report(self, name: str) -> str | None:
        row = self.con.execute("SELECT body FROM reports WHERE name=? ORDER BY created_at DESC LIMIT 1",
                               (name,)).fetchone()
        return None if row is None else str(row[0])

    def evidence_rows(self, sim_version: str, evidence_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM shadow_evidence WHERE sim_version=? AND evidence_version=? ORDER BY signal_id",
            (sim_version, evidence_version))]

    def minute_closes(self, instrument_key: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        """(candle END, close) of stored 1-minute candles ending in [start, end]."""
        lo, hi = (start - timedelta(minutes=1)).isoformat(), (end - timedelta(minutes=1)).isoformat()
        rows = self.con.execute("SELECT start_time, close FROM minute_candles WHERE instrument_key=? AND"
                                " interval_minutes=1 AND complete=1 AND start_time BETWEEN ? AND ?"
                                " ORDER BY start_time", (instrument_key, lo, hi))
        return [(datetime.fromisoformat(r[0]) + timedelta(minutes=1), float(r[1])) for r in rows]

    def shadow_rows(self, sim_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM shadow_outcomes WHERE sim_version=? ORDER BY generated_at, signal_id", (sim_version,))]

    def archived_days(self, kind: str) -> set[date]:
        rows = self.con.execute("SELECT session_date FROM archive_log WHERE kind=?", (kind,))
        return {date.fromisoformat(r[0]) for r in rows}

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


__all__ = ["ARCHIVE_COLUMNS", "DECISION_COLUMNS", "EVIDENCE_COLUMNS", "REGIME_COLUMNS", "SCHEMA_VERSION",
           "ArchivedQuote", "ChainRow", "QuoteBar", "ResearchStore", "ResearchStoreError"]
