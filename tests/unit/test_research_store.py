"""Research database, read-only trading-DB access and the recorded-quote summary."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from broker.upstox_mapping import parse_instrument
from core.clock import IST
from core.models import Candle
from research.collect.quotes import days_to_summarize, summarize_recorded_quotes
from research.store import SCHEMA_VERSION, ChainRow, ResearchStore, ResearchStoreError
from research.trading_readonly import (
    TradingDbUnavailableError,
    open_trading_db_readonly,
    quote_minute_bars,
    recorded_quote_days,
)
from tests.fakes import option_contract

T0 = datetime(2026, 11, 14, 18, 0, tzinfo=IST)
VIX = "NSE_INDEX|India VIX"


def daily(key: str, d: date, close: float, *, complete: bool = True) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=IST)
    return Candle(instrument_key=key, timeframe_minutes=1440, start=start, end=start + timedelta(days=1),
                  open=close, high=close + 1, low=close - 1, close=close, complete=complete)


def minute(key: str, at: datetime, close: float) -> Candle:
    return Candle(instrument_key=key, timeframe_minutes=1, start=at, end=at + timedelta(minutes=1), open=close,
                  high=close + 0.5, low=close - 0.5, close=close)


@pytest.fixture
def store(tmp_path: Path) -> ResearchStore:
    s = ResearchStore.open(tmp_path / "research.db", forbidden=[tmp_path / "trading.db"])
    yield s  # type: ignore[misc]
    s.close()


def test_open_creates_schema_once_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "research.db"
    ResearchStore.open(path).close()
    s = ResearchStore.open(path)
    assert [tuple(r) for r in s.con.execute("SELECT version FROM schema_version")] == [(SCHEMA_VERSION,)]
    s.close()


def test_open_refuses_a_newer_schema_and_non_db_paths(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    s = ResearchStore.open(path)
    s.con.execute("INSERT INTO schema_version VALUES (?, ?)", (SCHEMA_VERSION + 1, T0.isoformat()))
    s.con.commit()
    s.close()
    with pytest.raises(ResearchStoreError, match="version"):
        ResearchStore.open(path)
    with pytest.raises(ResearchStoreError, match=".db"):
        ResearchStore.open(tmp_path / "research.sqlite")


def test_daily_and_minute_candles_upsert(store: ResearchStore) -> None:
    assert store.upsert_daily([daily(VIX, date(2026, 11, 12), 12.0), daily(VIX, date(2026, 11, 13), 12.5)], at=T0) == 2
    store.upsert_daily([daily(VIX, date(2026, 11, 13), 13.0)], at=T0)  # corrected value replaces
    rows = store.con.execute("SELECT session_date, close FROM daily_candles ORDER BY 1").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-12", 12.0), ("2026-11-13", 13.0)]
    assert store.daily_sessions(VIX) == [date(2026, 11, 12), date(2026, 11, 13)]
    store.upsert_daily([daily(VIX, date(2026, 11, 14), 13.0, complete=False)], at=T0)
    assert date(2026, 11, 14) not in store.daily_sessions(VIX)

    at = datetime(2022, 1, 3, 9, 15, tzinfo=IST)
    assert store.upsert_minutes([minute(VIX, at, 17.0), minute(VIX, at + timedelta(minutes=1), 17.2)], at=T0) == 2
    assert store.count("minute_candles") == 2
    with pytest.raises(ResearchStoreError):
        store.upsert_daily([minute(VIX, at, 1.0)])
    with pytest.raises(ResearchStoreError):
        store.upsert_minutes([daily(VIX, date(2026, 11, 12), 12.0)])
    with pytest.raises(ValueError):
        store.count("trades")


def test_completed_ranges_only_count_ranges_fetched_after_they_ended(store: ResearchStore) -> None:
    a, b = date(2026, 10, 1), date(2026, 10, 31)
    store.log_fetch(VIX, "minutes", 1, a, b, 100, "OK", at=datetime(2026, 11, 1, 18, 0, tzinfo=IST))
    store.log_fetch(VIX, "minutes", 1, date(2026, 11, 1), date(2026, 11, 14), 50, "OK", at=T0)  # same day: open
    store.log_fetch(VIX, "minutes", 1, date(2026, 9, 1), date(2026, 9, 30), 0, "ERROR", "x", at=T0)
    assert store.completed_ranges(VIX, "minutes", 1) == {(a, b)}
    assert store.completed_ranges(VIX, "days", 1) == set()


def test_contracts_survive_and_keep_first_seen(store: ResearchStore) -> None:
    inst = [parse_instrument(option_contract(25100, "PE", "2026-11-17", key="NSE_FO|45394", lot=65))]
    store.upsert_contracts(inst, at=T0)
    store.upsert_contracts(inst, at=T0 + timedelta(days=1))
    row = store.contracts()["NSE_FO|45394"]
    assert (row["strike"], row["expiry"], row["instrument_type"], row["lot_size"]) == (25100.0, "2026-11-17", "PE", 65)
    assert row["first_seen"] == T0.isoformat() and row["last_seen"] == (T0 + timedelta(days=1)).isoformat()
    assert store.observed_expiries("NSE_INDEX|Nifty 50") == [date(2026, 11, 17)]


def test_chain_snapshot_keeps_raw_values(store: ResearchStore) -> None:
    rows = [ChainRow(25100.0, "CE", "NSE_FO|1", ltp=120.5, iv_raw=13.2, strike_pcr=0.9),
            ChainRow(25100.0, "PE", "NSE_FO|2", ltp=110.0, iv_raw=13.9, strike_pcr=0.9)]
    sid = store.save_chain_snapshot(taken_at=T0, underlying_key="NSE_INDEX|Nifty 50", expiry=date(2026, 11, 17),
                                    spot=25139.25, rows=rows)
    snap = store.con.execute("SELECT strikes, spot FROM chain_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
    assert tuple(snap) == (1, 25139.25)
    ivs = store.con.execute("SELECT option_type, iv_raw FROM chain_snapshot_rows ORDER BY 1").fetchall()
    assert [tuple(r) for r in ivs] == [("CE", 13.2), ("PE", 13.9)]


def test_observations_upsert(store: ResearchStore) -> None:
    store.record_observation("chain_iv_unit", "percent", "first", at=T0)
    store.record_observation("chain_iv_unit", "percent", "second", at=T0)
    assert store.observation("chain_iv_unit") == "percent" and store.observation("missing") is None


# --------------------------------------------------------------------------- #
# Read-only trading database + quote summary
# --------------------------------------------------------------------------- #
QUOTES_DDL = """CREATE TABLE option_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_key TEXT NOT NULL, exchange_time TEXT NOT NULL,
    received_at TEXT NOT NULL, ltp REAL NOT NULL, bid REAL, ask REAL, bid_qty INTEGER, ask_qty INTEGER,
    volume_today INTEGER, oi REAL, iv REAL, held INTEGER NOT NULL DEFAULT 0)"""


def _trading_db(path: Path, quotes: list[tuple[str, str, float, float, int]]) -> None:
    con = sqlite3.connect(path)
    con.execute(QUOTES_DDL)
    con.executemany("INSERT INTO option_quotes(instrument_key, exchange_time, received_at, ltp, bid, ask, iv, held,"
                    " oi, volume_today) VALUES (?, ?, ?, ?, ?-0.5, ?+0.5, ?, ?, 1000, 10)",
                    [(k, t, t, ltp, ltp, ltp, iv, held) for k, t, ltp, iv, held in quotes])
    con.commit()
    con.close()


QUOTES = [
    ("NSE_FO|1", "2026-11-12T09:17:05.100000+05:30", 100.0, 0.150, 0),
    ("NSE_FO|1", "2026-11-12T09:17:30.100000+05:30", 104.0, 0.152, 1),
    ("NSE_FO|1", "2026-11-12T09:17:55.100000+05:30", 98.0, 0.149, 1),
    ("NSE_FO|1", "2026-11-12T09:18:01.000000+05:30", 99.0, 0.148, 0),
    ("NSE_FO|2", "2026-11-12T09:17:10.000000+05:30", 80.0, 0.160, 0),
    ("NSE_FO|1", "2026-11-13T09:15:01.000000+05:30", 90.0, 0.140, 0),
]


def test_quote_minute_bars_from_a_read_only_connection(tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    _trading_db(db, QUOTES)
    con = open_trading_db_readonly(db)
    assert recorded_quote_days(con) == [date(2026, 11, 12), date(2026, 11, 13)]
    bars = {(b.instrument_key, b.minute_start): b for b in quote_minute_bars(con, date(2026, 11, 12))}
    assert len(bars) == 3
    b = bars[("NSE_FO|1", "2026-11-12T09:17:00+05:30")]
    assert (b.quotes, b.open_ltp, b.high_ltp, b.low_ltp, b.close_ltp) == (3, 100.0, 104.0, 98.0, 98.0)
    assert (b.last_iv, b.last_bid, b.last_ask, b.held_quotes) == (0.149, 97.5, 98.5, 2)
    con.close()


def test_open_read_only_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TradingDbUnavailableError):
        open_trading_db_readonly(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()


def test_days_to_summarize_always_redoes_the_latest_day() -> None:
    days = [date(2026, 11, 12), date(2026, 11, 13)]
    assert days_to_summarize(days, set()) == days
    assert days_to_summarize(days, set(days)) == [date(2026, 11, 13)]
    assert days_to_summarize([], set()) == []


def test_summarize_recorded_quotes_is_idempotent(tmp_path: Path, store: ResearchStore) -> None:
    db = tmp_path / "trading.db"
    _trading_db(db, QUOTES)
    con = open_trading_db_readonly(db)
    first = summarize_recorded_quotes(con, store, now=lambda: T0)
    assert first == {date(2026, 11, 12): 3, date(2026, 11, 13): 1}
    again = summarize_recorded_quotes(con, store, now=lambda: T0)
    assert again == {date(2026, 11, 13): 1}
    assert store.count("quote_minute_bars") == 4
    con.close()
    # the trading database was not changed
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0] == len(QUOTES)
    raw.close()
