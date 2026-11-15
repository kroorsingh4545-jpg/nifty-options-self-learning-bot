"""SQLite schema: tables, constraints, PAPER/LIVE separation, isolation triggers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.clock import now_ist
from core.enums import (
    LearningStage,
    ModelRunStatus,
    ModelType,
    OrderPurpose,
    OrderStatus,
    OrderType,
    RiskSeverity,
    StrategyStatus,
)
from data.storage.database import REQUIRED_TABLES, SCHEMA_VERSION, Database, DatabaseError

NOW = now_ist().isoformat()


def _order(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "client_order_id": "PAPER-orb_options-000000000001",
        "mode": "PAPER",
        "paper_order_id": "PO-1",
        "broker_order_id": None,
        "strategy_id": "orb_options",
        "trade_uid": None,
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "NIFTY 24500 CE",
        "side": "BUY",
        "order_type": "MARKET",
        "purpose": "ENTRY",
        "quantity": 75,
        "status": "CREATED",
        "filled_quantity": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


def _insert(db: Database, table: str, row: dict[str, Any]) -> int:
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    return int(db.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values())).lastrowid or 0)  # noqa: S608


def _trade(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trade_uid": "PAPER-T-orb_options-000000000001",
        "mode": "PAPER",
        "strategy_id": "orb_options",
        "strategy_version": "0.1.0",
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "NIFTY 24500 CE",
        "option_type": "CE",
        "strike": 24500,
        "expiry": "2026-11-05",
        "side": "BUY",
        "quantity": 75,
        "initial_stop_loss": 90.0,
        "stop_loss": 90.0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# Schema creation
# --------------------------------------------------------------------------- #


def test_all_required_tables_exist(db: Database) -> None:
    assert set(REQUIRED_TABLES) <= set(db.tables())
    assert db.current_schema_version() == SCHEMA_VERSION


def test_initialize_is_idempotent(db: Database) -> None:
    db.initialize()
    db.initialize()
    assert db.query_one("SELECT COUNT(*) AS n FROM schema_version")["n"] == 1  # type: ignore[index]


def test_foreign_keys_enabled(db: Database) -> None:
    assert db.query_one("PRAGMA foreign_keys")[0] == 1  # type: ignore[index]


def test_file_database_uses_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "sub" / "t.db")
    database.initialize()
    assert database.query_one("PRAGMA journal_mode")[0].lower() == "wal"  # type: ignore[index]
    database.close()


def test_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    database = Database(path)
    database.initialize()
    database.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION + 1, NOW))
    database.close()
    with pytest.raises(DatabaseError, match="newer"):
        Database(path).initialize()


def test_global_pnl_view_exists(db: Database) -> None:
    assert db.query_one("SELECT name FROM sqlite_master WHERE type='view' AND name='v_daily_pnl_global'")


# --------------------------------------------------------------------------- #
# Enum <-> CHECK constraint sync
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", list(OrderStatus))
def test_every_order_status_is_accepted(seeded_db: Database, status: OrderStatus) -> None:
    _insert(seeded_db, "orders", _order(status=status.value, client_order_id=f"PAPER-x-{status.value}",
                                        paper_order_id=f"PO-{status.value}"))


@pytest.mark.parametrize("purpose", list(OrderPurpose))
def test_every_order_purpose_is_accepted(seeded_db: Database, purpose: OrderPurpose) -> None:
    _insert(seeded_db, "orders", _order(purpose=purpose.value, client_order_id=f"PAPER-x-{purpose.value}",
                                        paper_order_id=f"PO-{purpose.value}"))


@pytest.mark.parametrize("otype", list(OrderType))
def test_every_order_type_is_accepted(seeded_db: Database, otype: OrderType) -> None:
    _insert(seeded_db, "orders", _order(order_type=otype.value, client_order_id=f"PAPER-x-{otype.value}",
                                        paper_order_id=f"PO-{otype.value}"))


def test_strategy_status_enum_matches_schema(db: Database) -> None:
    for i, st in enumerate(StrategyStatus):
        _insert(db, "strategies", {"strategy_id": f"s_{i}", "name": "n", "version": "1", "allocated_capital": 1,
                                   "status": st.value, "created_at": NOW, "updated_at": NOW})


def test_other_enums_match_schema(seeded_db: Database) -> None:
    for sev in RiskSeverity:
        _insert(seeded_db, "risk_events", {"mode": "PAPER", "event_type": "T", "severity": sev.value,
                                           "message": "m", "created_at": NOW})
    for i, stage in enumerate(LearningStage):
        _insert(seeded_db, "learning_events", {"event_uid": f"L-{i}", "strategy_id": "orb_options",
                                               "stage": stage.value, "created_at": NOW})
    for i, (mt, st) in enumerate(zip(list(ModelType) * 2, ModelRunStatus, strict=False)):
        _insert(seeded_db, "model_runs", {
            "run_id": f"R-{i}", "model_type": mt.value, "model_version": f"v{i}",
            "feature_schema_version": "1", "feature_schema_json": "{}", "training_start": "2026-01-01",
            "training_end": "2026-06-01", "artifact_path": "models/x", "status": st.value,
            "approved_by": "a.human" if st is ModelRunStatus.LIVE_APPROVED else None,
            "created_at": NOW, "updated_at": NOW, "data_source": "HISTORICAL",
            # v5: promotions record the autonomy level that allowed them
            "promoted_autonomy": "LIVE_CANDIDATE_APPROVAL",
        })


# --------------------------------------------------------------------------- #
# PAPER / LIVE identifier separation
# --------------------------------------------------------------------------- #


def test_paper_order_requires_paper_id(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(paper_order_id=None))


def test_paper_order_cannot_have_broker_id(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(broker_order_id="2401010000001"))


def test_live_order_cannot_have_paper_id(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(mode="LIVE", client_order_id="LIVE-orb_options-1"))


def test_live_order_accepts_broker_id(seeded_db: Database) -> None:
    _insert(seeded_db, "orders", _order(mode="LIVE", client_order_id="LIVE-orb_options-1", paper_order_id=None,
                                        broker_order_id="2401010000001"))


@pytest.mark.parametrize(("mode", "coid"), [("PAPER", "LIVE-orb_options-1"), ("LIVE", "PAPER-orb_options-1"),
                                            ("PAPER", "orb_options-1"), ("PAPER", "paper-orb_options-1")])
def test_client_order_id_prefix_must_match_mode(seeded_db: Database, mode: str, coid: str) -> None:
    extra = {"paper_order_id": "PO-1"} if mode == "PAPER" else {"paper_order_id": None}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(mode=mode, client_order_id=coid, **extra))


def test_trade_uid_prefix_must_match_mode(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "trades", _trade(mode="LIVE"))


def test_fill_identifier_rules(seeded_db: Database) -> None:
    oid = _insert(seeded_db, "orders", _order())
    base = {"order_id": oid, "mode": "PAPER", "strategy_id": "orb_options", "quantity": 75, "price": 100.0,
            "filled_at": NOW}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "fills", base)  # no paper_fill_id
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "fills", {**base, "paper_fill_id": "PF-1", "broker_fill_id": "X1"})
    _insert(seeded_db, "fills", {**base, "paper_fill_id": "PF-1"})


# --------------------------------------------------------------------------- #
# Isolation / immutability triggers
# --------------------------------------------------------------------------- #


def test_fill_must_match_order_mode(seeded_db: Database) -> None:
    oid = _insert(seeded_db, "orders", _order())
    with pytest.raises(sqlite3.IntegrityError, match="isolation"):
        _insert(seeded_db, "fills", {"order_id": oid, "mode": "LIVE", "strategy_id": "orb_options",
                                     "broker_fill_id": "X1", "quantity": 75, "price": 100.0, "filled_at": NOW})


def test_fill_must_match_order_strategy(seeded_db: Database) -> None:
    oid = _insert(seeded_db, "orders", _order())
    with pytest.raises(sqlite3.IntegrityError, match="isolation"):
        _insert(seeded_db, "fills", {"order_id": oid, "mode": "PAPER", "strategy_id": "vwap_ema",
                                     "paper_fill_id": "PF-1", "quantity": 75, "price": 100.0, "filled_at": NOW})


def test_order_must_match_trade_strategy(seeded_db: Database) -> None:
    _insert(seeded_db, "trades", _trade())
    with pytest.raises(sqlite3.IntegrityError, match="isolation"):
        _insert(seeded_db, "orders", _order(strategy_id="vwap_ema", client_order_id="PAPER-vwap_ema-1",
                                            trade_uid="PAPER-T-orb_options-000000000001"))
    _insert(seeded_db, "orders", _order(trade_uid="PAPER-T-orb_options-000000000001"))


def test_order_mode_and_strategy_are_immutable(seeded_db: Database) -> None:
    _insert(seeded_db, "orders", _order())
    for sql in ("UPDATE orders SET strategy_id = 'vwap_ema'", "UPDATE orders SET mode = 'LIVE'",
                "UPDATE orders SET paper_order_id = 'PO-2'"):
        with pytest.raises(sqlite3.IntegrityError):
            seeded_db.execute(sql)


def test_broker_order_id_immutable_once_set(seeded_db: Database) -> None:
    _insert(seeded_db, "orders", _order(mode="LIVE", client_order_id="LIVE-orb_options-1", paper_order_id=None))
    seeded_db.execute("UPDATE orders SET broker_order_id = 'B1'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        seeded_db.execute("UPDATE orders SET broker_order_id = 'B2'")


def test_fills_are_append_only(seeded_db: Database) -> None:
    oid = _insert(seeded_db, "orders", _order())
    _insert(seeded_db, "fills", {"order_id": oid, "mode": "PAPER", "strategy_id": "orb_options",
                                 "paper_fill_id": "PF-1", "quantity": 75, "price": 100.0, "filled_at": NOW})
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute("UPDATE fills SET price = 1")


def test_risk_events_are_append_only(seeded_db: Database) -> None:
    _insert(seeded_db, "risk_events", {"mode": "PAPER", "event_type": "KILL_SWITCH", "severity": "CRITICAL",
                                       "message": "m", "created_at": NOW})
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute("UPDATE risk_events SET severity = 'INFO'")
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute("DELETE FROM risk_events")


def test_unknown_strategy_rejected_by_foreign_key(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(strategy_id="ghost", client_order_id="PAPER-ghost-1"))


# --------------------------------------------------------------------------- #
# Safety constraints
# --------------------------------------------------------------------------- #


def test_filled_quantity_cannot_exceed_quantity(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "orders", _order(filled_quantity=100))


def test_open_position_requires_stop_loss(seeded_db: Database) -> None:
    pos = {"mode": "PAPER", "strategy_id": "orb_options", "instrument_key": "K", "trading_symbol": "S",
           "quantity": 75, "average_price": 100.0, "opened_at": NOW, "updated_at": NOW}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "positions", pos)
    _insert(seeded_db, "positions", {**pos, "stop_loss": 80.0})


def test_one_open_position_per_strategy_instrument(seeded_db: Database) -> None:
    pos = {"mode": "PAPER", "strategy_id": "orb_options", "instrument_key": "K", "trading_symbol": "S",
           "quantity": 75, "average_price": 100.0, "stop_loss": 80.0, "opened_at": NOW, "updated_at": NOW}
    _insert(seeded_db, "positions", pos)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "positions", pos)
    # Another strategy may hold the same instrument independently.
    _insert(seeded_db, "positions", {**pos, "strategy_id": "vwap_ema"})
    # And LIVE is separate from PAPER.
    _insert(seeded_db, "positions", {**pos, "mode": "LIVE"})


def test_closed_position_must_be_flat(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "positions", {"mode": "PAPER", "strategy_id": "orb_options", "instrument_key": "K",
                                         "trading_symbol": "S", "quantity": 75, "average_price": 100.0,
                                         "stop_loss": 80.0, "status": "CLOSED", "closed_at": NOW,
                                         "opened_at": NOW, "updated_at": NOW})


def test_trade_requires_stop_loss(seeded_db: Database) -> None:
    row = _trade()
    del row["stop_loss"]
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "trades", row)


def test_closed_trade_requires_exit_details(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "trades", _trade(status="CLOSED"))


def test_live_approved_model_requires_approver(seeded_db: Database) -> None:
    row = {"run_id": "R-1", "model_type": "SIGNAL_FILTER", "model_version": "v1", "feature_schema_version": "1",
           "feature_schema_json": "{}", "training_start": "2026-01-01", "training_end": "2026-06-01",
           "artifact_path": "models/x", "status": "LIVE_APPROVED", "created_at": NOW, "updated_at": NOW,
           "data_source": "HISTORICAL"}
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(seeded_db, "model_runs", row)


MODEL_ROW = {"model_type": "SIGNAL_FILTER", "feature_schema_version": "1", "feature_schema_json": "{}",
             "training_start": "2026-01-01", "training_end": "2026-06-01", "artifact_path": "models/x",
             "created_at": NOW, "updated_at": NOW}


@pytest.mark.parametrize("source", ["SYNTHETIC", "UNKNOWN"])
@pytest.mark.parametrize("status", ["VALIDATED", "PAPER_READY", "LIVE_CANDIDATE", "LIVE_APPROVED"])
def test_synthetic_models_can_never_be_validated_or_promoted(seeded_db: Database, source: str, status: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="HISTORICAL"):
        _insert(seeded_db, "model_runs", {**MODEL_ROW, "run_id": "R-ins", "model_version": "v1", "status": status,
                                          "approved_by": "a.human", "data_source": source})
    _insert(seeded_db, "model_runs", {**MODEL_ROW, "run_id": "R-up", "model_version": "v2", "data_source": source})
    with pytest.raises(sqlite3.IntegrityError, match="HISTORICAL"):
        seeded_db.execute("UPDATE model_runs SET status = ?, approved_by = 'a.human' WHERE run_id = 'R-up'", (status,))
    for ok in ("TRAINED", "REJECTED", "ARCHIVED"):
        seeded_db.execute("UPDATE model_runs SET status = ? WHERE run_id = 'R-up'", (ok,))


def test_model_data_source_is_immutable(seeded_db: Database) -> None:
    _insert(seeded_db, "model_runs", {**MODEL_ROW, "run_id": "R-1", "model_version": "v1", "data_source": "SYNTHETIC"})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        seeded_db.execute("UPDATE model_runs SET data_source = 'HISTORICAL' WHERE run_id = 'R-1'")
    assert seeded_db.query_one("SELECT data_source FROM model_runs")["data_source"] == "SYNTHETIC"  # type: ignore[index]


V3_MODEL_RUNS = """CREATE TABLE model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, model_type TEXT NOT NULL,
    model_version TEXT NOT NULL, strategy_id TEXT, feature_schema_version TEXT NOT NULL,
    feature_schema_json TEXT NOT NULL, training_start TEXT NOT NULL, training_end TEXT NOT NULL,
    hyperparameters_json TEXT NOT NULL DEFAULT '{}', metrics_json TEXT NOT NULL DEFAULT '{}',
    seeds_json TEXT NOT NULL DEFAULT '[]', environment_version TEXT, artifact_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'TRAINED', approved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def test_v3_database_is_migrated_to_current(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(V3_MODEL_RUNS)
    raw.execute("INSERT INTO model_runs (run_id, model_type, model_version, feature_schema_version, "
                "feature_schema_json, training_start, training_end, artifact_path, status, created_at, updated_at) "
                "VALUES ('OLD', 'SIGNAL_FILTER', 'v0', '1', '{}', '2026-01-01', '2026-02-01', 'm', 'PAPER_READY', "
                "'2026-02-01', '2026-02-01')")
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (3, '2026-02-01')")
    raw.commit()
    raw.close()
    db = Database(path)
    db.initialize()
    assert db.current_schema_version() == SCHEMA_VERSION == 7
    # an existing row of unknown provenance keeps its status but can never be promoted again
    assert db.query_one("SELECT data_source FROM model_runs WHERE run_id = 'OLD'")["data_source"] == "UNKNOWN"  # type: ignore[index]
    with pytest.raises(sqlite3.IntegrityError, match="HISTORICAL"):
        db.execute("UPDATE model_runs SET status = 'LIVE_CANDIDATE' WHERE run_id = 'OLD'")
    db.initialize()  # idempotent
    db.close()


def test_json_columns_must_be_valid_json(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "risk_events", {"mode": "PAPER", "event_type": "T", "severity": "INFO",
                                           "message": "m", "details_json": "{not json", "created_at": NOW})


def test_ml_confidence_bounded(seeded_db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(seeded_db, "trades", _trade(ml_confidence=1.5))


# --------------------------------------------------------------------------- #
# open_existing / verify_schema: for processes that are not the bot
# --------------------------------------------------------------------------- #


def _bot_db(path: Path) -> Path:
    db = Database(path)
    db.initialize()
    db.close()
    return path


def _set_version(path: Path, version: int) -> None:
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM schema_version")
    raw.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (version, now_ist().isoformat()))
    raw.commit()
    raw.close()


def test_open_existing_accepts_the_current_schema(tmp_path: Path) -> None:
    db = Database.open_existing(_bot_db(tmp_path / "bot.db"))
    assert db.verify_schema() == SCHEMA_VERSION
    db.close()


def test_open_existing_never_creates_a_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "missing.db"
    with pytest.raises(DatabaseError, match="does not exist"):
        Database.open_existing(path)
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize(("version", "msg"), [(SCHEMA_VERSION - 1, "older"), (SCHEMA_VERSION + 1, "newer")])
def test_open_existing_refuses_other_versions_without_migrating(tmp_path: Path, version: int, msg: str) -> None:
    path = _bot_db(tmp_path / "bot.db")
    _set_version(path, version)
    with pytest.raises(DatabaseError, match=msg):
        Database.open_existing(path)
    raw = sqlite3.connect(path)
    assert [r[0] for r in raw.execute("SELECT version FROM schema_version")] == [version]  # untouched
    raw.close()


def test_open_existing_refuses_a_foreign_or_incomplete_database(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("CREATE TABLE x (a INTEGER)")
    raw.commit()
    raw.close()
    with pytest.raises(DatabaseError, match="not created by the bot"):
        Database.open_existing(foreign)
    path = _bot_db(tmp_path / "bot.db")
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE runtime_state")
    raw.commit()
    raw.close()
    with pytest.raises(DatabaseError, match="missing tables"):
        Database.open_existing(path)
