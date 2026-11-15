"""Decision-engine D1, research side (SYNTHETIC data only): the raw quote
archive, the data-quality audit and the in-session chain-snapshot schedule."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from broker.errors import BrokerError
from core.clock import IST
from core.enums import TradingMode
from data.storage.database import Database
from data.storage.repositories import Repositories, SignalRow
from research.audit.data_quality import QualityThresholds, consecutive_clean, day_quality, render_text
from research.collect.archive import archive_recorded_quotes
from research.collect.chain import SnapshotResult, due_times, run_chain_schedule
from research.store import ResearchStore
from research.trading_readonly import open_trading_db_readonly, raw_quotes, recorded_days, recorded_signals
from tests.synthetic import tick
from tests.unit.test_decision_d1_recording import rich_tick, signal

DAY = date(2026, 11, 17)
INDEX, FUT, VIX = "NSE_INDEX|Nifty 50", "NSE_FO|FUT", "NSE_INDEX|India VIX"
T0 = datetime(2026, 11, 17, 18, 0, tzinfo=IST)


def t(h: int, m: int, s: float = 0.0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(h, m), tzinfo=IST) + timedelta(seconds=s)


@pytest.fixture
def store(tmp_path: Path) -> ResearchStore:
    s = ResearchStore.open(tmp_path / "research.db", forbidden=[tmp_path / "trading.db"])
    yield s  # type: ignore[misc]
    s.close()


def bot_db(path: Path, *, days: tuple[date, ...] = (DAY,), every: int = 5) -> None:
    """A v7 trading database as the bot writes it: two option contracts and the
    index / future / VIX, sampled every ``every`` seconds over the whole session."""
    db = Database(path)
    db.initialize()
    h = Repositories(db, TradingMode.PAPER).market_history
    for d in days:
        opts, mkts = [], []
        for s in range(0, 6 * 3600 + 15 * 60 + 1, every):  # 09:15:00 .. 15:30:00
            at = t(9, 15, s, day=d)
            opts += [(rich_tick("NSE_FO|1CE", 100.0 + (s % 7) * 0.05, at), False),
                     (rich_tick("NSE_FO|1PE", 90.0, at), False)]
            mkts += [tick(INDEX, 24500.0 + (s % 11), at, spread=0.0).model_copy(update={"bid": None, "ask": None}),
                     rich_tick(FUT, 24560.0, at), tick(VIX, 13.0, at).model_copy(update={"bid": None, "ask": None})]
        h.save_quotes(opts)
        h.save_market_quotes(mkts)
    h.save_signals([SignalRow(signal("SIG-1", t(9, 50)), TradingMode.PAPER, decided=True, approved=False)])
    db.close()


# --------------------------------------------------------------------------- #
# Read-only access to the v7 tables
# --------------------------------------------------------------------------- #


def test_raw_quotes_read_v7_rows_read_only(tmp_path: Path) -> None:
    path = tmp_path / "trading.db"
    bot_db(path)
    con = open_trading_db_readonly(path)
    assert recorded_days(con, "option_quotes") == [DAY] == recorded_days(con, "market_quotes")
    rows = raw_quotes(con, DAY, "option_quotes")
    assert rows[0]["delta"] == 0.51 and rows[0]["tbq"] == 120_000.0
    assert [r["signal_id"] for r in recorded_signals(con, DAY)] == ["SIG-1"]
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM option_quotes")
    con.close()


def test_a_v6_trading_database_reads_with_nulls_for_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "trading.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE option_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_key TEXT, "
                "exchange_time TEXT, received_at TEXT, ltp REAL, bid REAL, ask REAL, bid_qty INTEGER, "
                "ask_qty INTEGER, volume_today INTEGER, oi REAL, iv REAL, held INTEGER)")
    raw.execute("INSERT INTO option_quotes(instrument_key, exchange_time, received_at, ltp, held) VALUES "
                "('NSE_FO|1', '2026-11-13T10:00:00+05:30', '2026-11-13T10:00:00.2+05:30', 100.0, 0)")
    raw.commit()
    raw.close()
    con = open_trading_db_readonly(path)
    (row,) = raw_quotes(con, date(2026, 11, 13), "option_quotes")
    assert row["ltp"] == 100.0 and row["delta"] is None and row["tbq"] is None
    assert raw_quotes(con, date(2026, 11, 13), "market_quotes") == []  # table not there yet
    assert recorded_signals(con, date(2026, 11, 13)) == []
    con.close()


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #


def test_the_archive_copies_raw_quotes_and_is_idempotent(tmp_path: Path, store: ResearchStore) -> None:
    path = tmp_path / "trading.db"
    day2 = DAY + timedelta(days=1)
    bot_db(path, days=(DAY, day2), every=60)
    con = open_trading_db_readonly(path)
    first = archive_recorded_quotes(con, store, now=lambda: T0)
    per_day_opt, per_day_mkt = 2 * 376, 3 * 376
    assert first == {(DAY, "OPT"): per_day_opt, (day2, "OPT"): per_day_opt,
                     (DAY, "MKT"): per_day_mkt, (day2, "MKT"): per_day_mkt}
    again = archive_recorded_quotes(con, store, now=lambda: T0)  # only the latest day is redone
    assert again == {(day2, "OPT"): per_day_opt, (day2, "MKT"): per_day_mkt}
    assert store.count("quote_archive") == 2 * (per_day_opt + per_day_mkt)
    row = store.con.execute("SELECT * FROM quote_archive WHERE kind='OPT' LIMIT 1").fetchone()
    assert (row["delta"], row["tbq"], row["tsq"], row["ltq"]) == (0.51, 120_000.0, 95_000.0, 65)
    assert row["exchange_ms"] == int(t(9, 15).timestamp() * 1000)
    con.close()
    raw = sqlite3.connect(path)  # the trading database was not changed
    assert raw.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0] == 2 * per_day_opt
    raw.close()


def test_the_archive_refuses_mixed_kinds(store: ResearchStore) -> None:
    from research.store import ArchivedQuote

    with pytest.raises(ValueError):
        store.archive_quotes(DAY, "OPT", [ArchivedQuote("K", 1, 1, "MKT", 1.0)])


# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #


def _rows(path: Path, day: date = DAY) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    con = open_trading_db_readonly(path)
    try:
        return raw_quotes(con, day, "option_quotes"), raw_quotes(con, day, "market_quotes"), \
            recorded_signals(con, day)
    finally:
        con.close()


def _quality(opts: list[dict[str, Any]], mkts: list[dict[str, Any]], sigs: list[dict[str, Any]],
             **kw: Any) -> dict[str, Any]:
    return day_quality(DAY, opts, mkts, sigs, index_key=INDEX, futures_keys={FUT}, vix_key=VIX,
                       expected_option_contracts=kw.pop("expected", 2), th=QualityThresholds(**kw))


def test_a_complete_session_is_clean(tmp_path: Path) -> None:
    bot_db(tmp_path / "trading.db")
    rep = _quality(*_rows(tmp_path / "trading.db"))
    assert rep["clean"], rep["problems"]
    tot = rep["totals"]
    assert tot["option_instruments"] == 2 and tot["signals"] == 1 and tot["missing_greeks_share"] == 0.0
    assert tot["option_window_completeness"] == 1.0 and tot["session_slots"] == 4500
    assert rep["market"][INDEX]["session_completeness"] == 1.0
    assert "CLEAN" in render_text(rep)[0]


def test_gaps_missing_fields_and_bad_quotes_are_flagged(tmp_path: Path) -> None:
    bot_db(tmp_path / "trading.db")
    opts, mkts, sigs = _rows(tmp_path / "trading.db")
    ce = [r for r in opts if r["instrument_key"] == "NSE_FO|1CE"]
    noon = [r for r in ce if "T12:" in str(r["received_at"])]
    gone = {id(r) for r in noon}
    opts = [r for r in opts if id(r) not in gone]  # an hour of silence
    for r in opts[:200]:
        r.update(delta=None, tbq=None)  # Greeks and resting quantity missing
    opts[300].update(bid=101.0, ask=100.0)  # crossed
    opts[301].update(bid=50.0, ask=60.0)  # 18 % spread
    mkts = [r for r in mkts if r["instrument_key"] != VIX
            and not (r["instrument_key"] == FUT and "T13:" in str(r["received_at"]))]  # VIX gone, future silent
    rep = _quality(opts, mkts, sigs)
    text = "\n".join(rep["problems"])
    assert not rep["clean"]
    for expected in ("option window completeness 0.920", "greeks missing", "tbq_tsq missing", "crossed",
                     f"{FUT}: gap of 3605", f"{FUT}: completeness", "no India VIX quotes"):
        assert expected in text, expected
    assert "NSE_FO|1CE: gap" not in text  # an option leaving the window is not a feed gap
    assert rep["options"]["NSE_FO|1CE"]["max_gap_seconds"] == 3605.0  # ... but it is reported


def test_a_contract_that_moves_in_and_out_of_the_window_is_not_a_gap(tmp_path: Path) -> None:
    bot_db(tmp_path / "trading.db")
    opts, mkts, sigs = _rows(tmp_path / "trading.db")
    # until 12:00 the window holds 1CE; after it, a new contract 2CE takes its place
    moved = [dict(r, instrument_key="NSE_FO|2CE") if r["instrument_key"] == "NSE_FO|1CE"
             and str(r["received_at"]) >= t(12, 0).isoformat() else r for r in opts]
    rep = _quality(moved, mkts, sigs)
    assert rep["clean"], rep["problems"]
    assert rep["totals"]["option_instruments"] == 3 and rep["totals"]["option_window_completeness"] == 1.0


def test_a_late_start_and_a_clock_offset_are_flagged(tmp_path: Path) -> None:
    bot_db(tmp_path / "trading.db")
    opts, mkts, sigs = _rows(tmp_path / "trading.db")
    late = [r for r in mkts if str(r["received_at"]) >= t(10, 0).isoformat()]
    for r in late:
        r["received_at"] = (datetime.fromisoformat(str(r["received_at"])) + timedelta(seconds=7)).isoformat()
    rep = _quality(opts, late, sigs)
    text = "\n".join(rep["problems"])
    assert "first recorded at" in text and "clock offset +7.00 s" in text


def test_the_last_price_outside_the_quote_is_measured_not_failed(tmp_path: Path) -> None:
    bot_db(tmp_path / "trading.db")
    opts, mkts, sigs = _rows(tmp_path / "trading.db")
    for r in opts[::2]:
        r["ltp"] = float(r["ask"]) + 1.0  # the feed's price runs ahead of its quote
    rep = _quality(opts, mkts, sigs)
    assert rep["clean"] and rep["totals"]["ltp_outside_quote_share"] == pytest.approx(0.5, abs=0.01)


def test_consecutive_clean_counts_back_from_the_latest_day() -> None:
    reps = [{"day": "2026-11-12", "clean": True}, {"day": "2026-11-13", "clean": False},
            {"day": "2026-11-16", "clean": True}, {"day": "2026-11-17", "clean": True}]
    assert consecutive_clean(reps) == 2
    assert consecutive_clean([]) == 0


# --------------------------------------------------------------------------- #
# Chain-snapshot schedule
# --------------------------------------------------------------------------- #

SCHEDULE = (time(9, 20), time(11, 0), time(13, 30), time(15, 20))


def test_due_times_keep_a_slightly_late_start() -> None:
    assert due_times(SCHEDULE, t(9, 25)) == [t(9, 20), t(11, 0), t(13, 30), t(15, 20)]
    assert due_times(SCHEDULE, t(12, 0)) == [t(13, 30), t(15, 20)]
    assert due_times(SCHEDULE, t(16, 0)) == []


async def test_the_schedule_sleeps_until_each_time_and_survives_an_error() -> None:
    clock = {"now": t(9, 0)}
    slept: list[float] = []
    reports: list[Any] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    async def take() -> list[SnapshotResult]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BrokerError("rate limited")
        return [SnapshotResult(DAY, 1, 10, 24500.0)]

    taken = await run_chain_schedule(SCHEDULE, now=lambda: clock["now"], sleep=sleep, take=take,
                                     report=lambda at, res: reports.append((at, res)))
    assert taken == 3 and calls == 4
    assert slept[0] == 20 * 60 and len(slept) == 4
    assert isinstance(reports[1][1], str) and "rate limited" in reports[1][1]


def test_the_expected_option_window_follows_the_bot_configuration() -> None:
    from config.loader import load_config
    from research.__main__ import expected_option_contracts

    cfg = load_config(use_default_env_file=False)
    assert expected_option_contracts(cfg) == 2 * 11 + 2 * 7  # nearest +/- 5 and next expiry +/- 3, CE and PE
    off = cfg.model_copy(update={"settings": cfg.settings.model_copy(update={
        "market_recording": cfg.settings.market_recording.model_copy(update={"next_expiry_band": 0})})})
    assert expected_option_contracts(off) == 22
