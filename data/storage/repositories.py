"""Data-access layer.

Mode-scoped repositories (orders, fills, positions, trades, daily P&L, risk
events, feature snapshots) are bound to ONE trading mode at construction.
Every read filters by that mode and every write is rejected if the record's
mode differs, so PAPER code cannot read or write LIVE rows and vice versa.
Strategy isolation is preserved by always scoping by ``strategy_id``.

Clock: every repository writes timestamps (``updated_at``, ``closed_at``,
``exit_time``, and any record timestamp the caller left at its default) from
ONE injected clock, ``Repositories(clock=...)``. The bot binds it to its own
``now_fn`` (``Repositories.with_clock``), so a backtest on simulated time
persists simulated timestamps only; the wall clock is just the default.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta
from sqlite3 import Row
from typing import Any, TypeVar

from core.autonomy import PAPER_PROMOTION_LEVELS
from core.clock import ensure_ist, now_ist
from core.enums import (
    REAL_DATA_ONLY_STATUSES,
    REAL_EVIDENCE_VERSION_STATUSES,
    AutonomyLevel,
    ControlCommandStatus,
    ControlCommandType,
    EvidenceSource,
    ModelDataSource,
    ModelRunStatus,
    ModelType,
    OrderStatus,
    PositionStatus,
    StrategyStatus,
    StrategyVersionStatus,
    TradeStatus,
    TradingMode,
)
from core.models import Candle, Tick
from data.storage.database import Database
from data.storage.records import (
    ControlCommandRecord,
    DailyPnlRecord,
    FeatureSnapshotRecord,
    FillRecord,
    LearningEventRecord,
    ModelRunRecord,
    OrderRecord,
    PositionRecord,
    Record,
    RiskEventRecord,
    RLDecisionRecord,
    StrategyDeploymentRecord,
    StrategyRecord,
    StrategyVersionRecord,
    TradeJournalRecord,
    TradeRecord,
)

R = TypeVar("R", bound=Record)
Clock = Callable[[], datetime]

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class ModeIsolationError(RuntimeError):
    """A record of one trading mode was passed to a repository of another."""


class NotFoundError(LookupError):
    pass


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


# SQL builders: values are always bound parameters; table/column names come
# from Pydantic field names and are validated by _ident() (hence noqa: S608).


def _insert(db: Database, table: str, row: dict[str, Any]) -> int:
    cols = [_ident(c) for c in row]
    sql = f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"  # noqa: S608
    cur = db.execute(sql, tuple(row.values()))
    return int(cur.lastrowid or 0)


def _upsert(db: Database, table: str, row: dict[str, Any], conflict: tuple[str, ...], keep: tuple[str, ...]) -> None:
    """INSERT ... ON CONFLICT DO UPDATE, leaving ``conflict`` + ``keep`` columns untouched."""
    cols = [_ident(c) for c in row]
    updates = [f"{c} = excluded.{c}" for c in cols if c not in conflict and c not in keep]
    sql = (
        f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "  # noqa: S608
        f"ON CONFLICT({', '.join(_ident(c) for c in conflict)}) DO UPDATE SET {', '.join(updates)}"
    )
    db.execute(sql, tuple(row.values()))


def _to_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def _stamp(record: R, clock: Clock) -> R:
    """Set every clock-stamped field the caller did not pass explicitly to
    the repository clock (explicit values are kept)."""
    unset = [n for n in record.clock_fields() if n not in record.model_fields_set]
    if not unset:
        return record
    now = ensure_ist(clock())
    return record.model_copy(update=dict.fromkeys(unset, now))


class _Clocked:
    def __init__(self, db: Database, *, clock: Clock = now_ist) -> None:
        self.db = db
        self.clock = clock

    def now(self) -> datetime:
        """The repository clock's current time (IST)."""
        return ensure_ist(self.clock())


class _ModeScoped(_Clocked):
    def __init__(self, db: Database, mode: TradingMode, *, clock: Clock = now_ist) -> None:
        super().__init__(db, clock=clock)
        self.mode = TradingMode(mode)

    def _guard(self, record: Any) -> None:
        if TradingMode(record.mode) is not self.mode:
            raise ModeIsolationError(
                f"{type(self).__name__} is bound to {self.mode.value}; refusing {record.mode} record"
            )

    def _update(self, table: str, where: dict[str, Any], fields: dict[str, Any]) -> int:
        if not fields:
            return 0
        sets = ", ".join(f"{_ident(k)} = ?" for k in fields)
        conds = " AND ".join(f"{_ident(k)} = ?" for k in where)
        params = (
            *(_to_value(v) for v in fields.values()),
            self.mode.value,
            *(_to_value(v) for v in where.values()),
        )
        sql = f"UPDATE {_ident(table)} SET {sets} WHERE mode = ? AND {conds}"  # noqa: S608
        cur = self.db.execute(sql, params)
        return int(cur.rowcount)


# --------------------------------------------------------------------------- #
# strategies (not mode-scoped: strategy definitions are shared config)
# --------------------------------------------------------------------------- #


class StrategyRepository(_Clocked):
    def upsert(self, record: StrategyRecord) -> None:
        record = _stamp(record, self.clock)
        _upsert(self.db, "strategies", record.to_row(), conflict=("strategy_id",), keep=("created_at",))

    def get(self, strategy_id: str) -> StrategyRecord | None:
        row = self.db.query_one("SELECT * FROM strategies WHERE strategy_id = ?", (strategy_id,))
        return StrategyRecord.from_row(row) if row else None

    def list_all(self) -> list[StrategyRecord]:
        return [StrategyRecord.from_row(r) for r in self.db.query("SELECT * FROM strategies ORDER BY strategy_id")]

    def set_status(self, strategy_id: str, status: StrategyStatus, *, at: datetime) -> None:
        """``at`` is the caller's clock: the new-day reset compares it with the
        same clock, so simulated time (backtests) stays consistent."""
        cur = self.db.execute(
            "UPDATE strategies SET status = ?, updated_at = ? WHERE strategy_id = ?",
            (StrategyStatus(status).value, at.isoformat(), strategy_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"strategy {strategy_id!r} not found")


# --------------------------------------------------------------------------- #
# orders / fills
# --------------------------------------------------------------------------- #


class OrderRepository(_ModeScoped):
    def insert(self, order: OrderRecord) -> int:
        self._guard(order)
        return _insert(self.db, "orders", _stamp(order, self.clock).to_row(exclude={"id"}))

    def get(self, client_order_id: str) -> OrderRecord | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE mode = ? AND client_order_id = ?", (self.mode.value, client_order_id)
        )
        return OrderRecord.from_row(row) if row else None

    def get_by_id(self, order_id: int) -> OrderRecord | None:
        row = self.db.query_one("SELECT * FROM orders WHERE mode = ? AND id = ?", (self.mode.value, order_id))
        return OrderRecord.from_row(row) if row else None

    def list_for_strategy(
        self, strategy_id: str, statuses: Iterable[OrderStatus] | None = None
    ) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE mode = ? AND strategy_id = ?"
        params: list[Any] = [self.mode.value, strategy_id]
        status_list = [OrderStatus(s).value for s in statuses] if statuses is not None else None
        if status_list is not None:
            sql += f" AND status IN ({', '.join('?' for _ in status_list)})"
            params.extend(status_list)
        return [OrderRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def list_all(self) -> list[OrderRecord]:
        """Every order for this mode, oldest first."""
        rows = self.db.query("SELECT * FROM orders WHERE mode = ? ORDER BY id", (self.mode.value,))
        return [OrderRecord.from_row(r) for r in rows]

    def list_open(self) -> list[OrderRecord]:
        """All non-terminal orders (any strategy) for this mode."""
        rows = self.db.query(
            "SELECT * FROM orders WHERE mode = ? AND status NOT IN ('COMPLETE','REJECTED','CANCELLED') ORDER BY id",
            (self.mode.value,),
        )
        return [OrderRecord.from_row(r) for r in rows]

    def list_for_trade(self, trade_uid: str) -> list[OrderRecord]:
        rows = self.db.query("SELECT * FROM orders WHERE mode = ? AND trade_uid = ? ORDER BY id",
                             (self.mode.value, trade_uid))
        return [OrderRecord.from_row(r) for r in rows]

    def update_status(
        self,
        client_order_id: str,
        status: OrderStatus,
        *,
        filled_quantity: int | None = None,
        average_price: float | None = None,
        status_message: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        """Persist a status change. Lifecycle-transition rules live in
        ``execution/`` (Phase 3); the schema still enforces quantity bounds and
        identifier immutability."""
        if broker_order_id is not None and self.mode is TradingMode.PAPER:
            raise ModeIsolationError("PAPER orders cannot be given a broker_order_id")
        fields: dict[str, Any] = {"status": OrderStatus(status), "updated_at": self.now()}
        if filled_quantity is not None:
            fields["filled_quantity"] = filled_quantity
        if average_price is not None:
            fields["average_price"] = average_price
        if status_message is not None:
            fields["status_message"] = status_message
        if broker_order_id is not None:
            fields["broker_order_id"] = broker_order_id
        if self._update("orders", {"client_order_id": client_order_id}, fields) == 0:
            raise NotFoundError(f"{self.mode.value} order {client_order_id!r} not found")


class FillRepository(_ModeScoped):
    def insert(self, fill: FillRecord) -> int:
        self._guard(fill)
        return _insert(self.db, "fills", _stamp(fill, self.clock).to_row(exclude={"id"}))

    def list_all(self) -> list[FillRecord]:
        """Every fill for this mode, oldest first."""
        rows = self.db.query("SELECT * FROM fills WHERE mode = ? ORDER BY filled_at, id", (self.mode.value,))
        return [FillRecord.from_row(r) for r in rows]

    def list_for_order(self, order_id: int) -> list[FillRecord]:
        rows = self.db.query(
            "SELECT * FROM fills WHERE mode = ? AND order_id = ? ORDER BY id", (self.mode.value, order_id)
        )
        return [FillRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# positions / trades
# --------------------------------------------------------------------------- #


class PositionRepository(_ModeScoped):
    def insert(self, position: PositionRecord) -> int:
        self._guard(position)
        return _insert(self.db, "positions", _stamp(position, self.clock).to_row(exclude={"id"}))

    def get_open(self, strategy_id: str, instrument_key: str) -> PositionRecord | None:
        row = self.db.query_one(
            "SELECT * FROM positions WHERE mode = ? AND strategy_id = ? AND instrument_key = ? AND status = 'OPEN'",
            (self.mode.value, strategy_id, instrument_key),
        )
        return PositionRecord.from_row(row) if row else None

    def list_open(self, strategy_id: str | None = None) -> list[PositionRecord]:
        sql = "SELECT * FROM positions WHERE mode = ? AND status = 'OPEN'"
        params: list[Any] = [self.mode.value]
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        return [PositionRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def update(self, position_id: int, **fields: Any) -> None:
        allowed = {
            "quantity", "average_price", "last_price", "realized_pnl", "unrealized_pnl",
            "stop_loss", "target", "trailing_stop", "status", "closed_at",
        }  # fmt: skip
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update position fields: {sorted(bad)}")
        fields["updated_at"] = self.now()
        if self._update("positions", {"id": position_id}, fields) == 0:
            raise NotFoundError(f"{self.mode.value} position {position_id} not found")

    def close(self, position_id: int, realized_pnl: float) -> None:
        self.update(
            position_id, quantity=0, status=PositionStatus.CLOSED, closed_at=self.now(), realized_pnl=realized_pnl,
            unrealized_pnl=0.0,
        )


class TradeRepository(_ModeScoped):
    def insert(self, trade: TradeRecord) -> int:
        self._guard(trade)
        return _insert(self.db, "trades", _stamp(trade, self.clock).to_row(exclude={"id"}))

    def get(self, trade_uid: str) -> TradeRecord | None:
        row = self.db.query_one(
            "SELECT * FROM trades WHERE mode = ? AND trade_uid = ?", (self.mode.value, trade_uid)
        )
        return TradeRecord.from_row(row) if row else None

    def list_for_strategy(self, strategy_id: str, status: TradeStatus | None = None) -> list[TradeRecord]:
        sql = "SELECT * FROM trades WHERE mode = ? AND strategy_id = ?"
        params: list[Any] = [self.mode.value, strategy_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(TradeStatus(status).value)
        return [TradeRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    _UPDATABLE = frozenset({
        "quantity", "entry_price", "entry_time", "stop_loss", "target", "status", "exit_reason", "charges",
        "regime", "ml_confidence",
    })

    def update(self, trade_uid: str, **fields: Any) -> None:
        """Update whitelisted fields. The schema additionally refuses to widen
        a long trade's stop-loss and to change identity columns."""
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"cannot update trade fields: {sorted(bad)}")
        fields["updated_at"] = self.now()
        if self._update("trades", {"trade_uid": trade_uid}, fields) == 0:
            raise NotFoundError(f"{self.mode.value} trade {trade_uid!r} not found")

    def list_open(self) -> list[TradeRecord]:
        rows = self.db.query("SELECT * FROM trades WHERE mode = ? AND status = 'OPEN' ORDER BY id", (self.mode.value,))
        return [TradeRecord.from_row(r) for r in rows]

    def list_closed_on(self, day: date) -> list[TradeRecord]:
        """Trades closed on this (IST) trading day, oldest exit first."""
        rows = self.db.query(
            "SELECT * FROM trades WHERE mode = ? AND status = 'CLOSED' AND substr(exit_time, 1, 10) = ? "
            "ORDER BY exit_time, id", (self.mode.value, day.isoformat()))
        return [TradeRecord.from_row(r) for r in rows]

    def count_entries_on(self, strategy_id: str, day: date) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM trades WHERE mode = ? AND strategy_id = ? AND substr(entry_time, 1, 10) = ?",
            (self.mode.value, strategy_id, day.isoformat()),
        )
        return int(row["n"]) if row else 0

    def count_created_by_option_type(self, strategy_id: str, day: date) -> dict[str, int]:
        """Trades the strategy opened on ``day`` (by creation time, whether or
        not the entry filled), per option type: an approved entry counts."""
        rows = self.db.query(
            "SELECT option_type, COUNT(*) AS n FROM trades WHERE mode = ? AND strategy_id = ? "
            "AND substr(created_at, 1, 10) = ? GROUP BY option_type",
            (self.mode.value, strategy_id, day.isoformat()),
        )
        return {str(r["option_type"]): int(r["n"]) for r in rows}

    def close(
        self,
        trade_uid: str,
        *,
        exit_price: float,
        exit_reason: str,
        gross_pnl: float,
        charges: float,
        r_multiple: float | None = None,
    ) -> None:
        now = self.now()
        fields = {
            "exit_price": exit_price,
            "exit_time": now,
            "exit_reason": exit_reason,
            "gross_pnl": gross_pnl,
            "charges": charges,
            "net_pnl": gross_pnl - charges,
            "r_multiple": r_multiple,
            "status": TradeStatus.CLOSED,
            "updated_at": now,
        }
        if self._update("trades", {"trade_uid": trade_uid}, fields) == 0:
            raise NotFoundError(f"{self.mode.value} trade {trade_uid!r} not found")


# --------------------------------------------------------------------------- #
# daily P&L / risk events / features
# --------------------------------------------------------------------------- #


class DailyPnlRepository(_ModeScoped):
    def upsert(self, record: DailyPnlRecord) -> None:
        self._guard(record)
        record = _stamp(record, self.clock)
        _upsert(self.db, "daily_pnl", record.to_row(), conflict=("trade_date", "mode", "strategy_id"), keep=())

    def get(self, day: date, strategy_id: str) -> DailyPnlRecord | None:
        row = self.db.query_one(
            "SELECT * FROM daily_pnl WHERE trade_date = ? AND mode = ? AND strategy_id = ?",
            (day.isoformat(), self.mode.value, strategy_id),
        )
        return DailyPnlRecord.from_row(row) if row else None

    def list_for_day(self, day: date) -> list[DailyPnlRecord]:
        rows = self.db.query(
            "SELECT * FROM daily_pnl WHERE trade_date = ? AND mode = ? ORDER BY strategy_id",
            (day.isoformat(), self.mode.value),
        )
        return [DailyPnlRecord.from_row(r) for r in rows]

    def global_for_day(self, day: date) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM v_daily_pnl_global WHERE trade_date = ? AND mode = ?", (day.isoformat(), self.mode.value)
        )
        return dict(row) if row else None


class RiskEventRepository(_ModeScoped):
    def insert(self, event: RiskEventRecord) -> int:
        self._guard(event)
        return _insert(self.db, "risk_events", _stamp(event, self.clock).to_row(exclude={"id"}))

    def recent(self, limit: int = 50, strategy_id: str | None = None) -> list[RiskEventRecord]:
        sql = "SELECT * FROM risk_events WHERE mode = ?"
        params: list[Any] = [self.mode.value]
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [RiskEventRecord.from_row(r) for r in self.db.query(sql, params)]

    def mentioning(self, text: str, strategy_id: str | None = None, *, since: datetime | None = None,
                   until: datetime | None = None) -> list[RiskEventRecord]:
        """Events whose message or details mention ``text`` (e.g. a trade uid),
        oldest first. ``since`` / ``until`` (inclusive) bound the search to a
        time window so the (mode, created_at) index is used instead of a scan
        of the whole table."""
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = "SELECT * FROM risk_events WHERE mode = ?"
        params: list[Any] = [self.mode.value]
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(ensure_ist(since).isoformat())
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(ensure_ist(until).isoformat())
        sql += " AND (message LIKE ? ESCAPE '\\' OR details_json LIKE ? ESCAPE '\\')"
        params += [like, like]
        if strategy_id is not None:
            sql += " AND (strategy_id = ? OR strategy_id IS NULL)"
            params.append(strategy_id)
        return [RiskEventRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def list_by_types(self, event_types: Sequence[str]) -> list[RiskEventRecord]:
        """Every event of the given types for this mode, oldest first."""
        types = list(event_types)
        if not types:
            return []
        marks = ",".join("?" * len(types))
        rows = self.db.query(f"SELECT * FROM risk_events WHERE mode = ? AND event_type IN ({marks}) ORDER BY id",  # noqa: S608
                             [self.mode.value, *types])
        return [RiskEventRecord.from_row(r) for r in rows]


class FeatureSnapshotRepository(_ModeScoped):
    def insert(self, snapshot: FeatureSnapshotRecord) -> int:
        self._guard(snapshot)
        return _insert(self.db, "feature_snapshots", _stamp(snapshot, self.clock).to_row(exclude={"id"}))

    def list_for_trade(self, trade_uid: str) -> list[FeatureSnapshotRecord]:
        rows = self.db.query(
            "SELECT * FROM feature_snapshots WHERE mode = ? AND trade_uid = ? ORDER BY snapshot_time, id",
            (self.mode.value, trade_uid),
        )
        return [FeatureSnapshotRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# model runs / learning events (not mode-scoped: research artefacts)
# --------------------------------------------------------------------------- #


class ModelRunRepository(_Clocked):
    def insert(self, run: ModelRunRecord) -> int:
        return _insert(self.db, "model_runs", _stamp(run, self.clock).to_row(exclude={"id"}))

    def get(self, run_id: str) -> ModelRunRecord | None:
        row = self.db.query_one("SELECT * FROM model_runs WHERE run_id = ?", (run_id,))
        return ModelRunRecord.from_row(row) if row else None

    def list(self, model_type: ModelType | None = None, strategy_id: str | None = None) -> list[ModelRunRecord]:
        sql = "SELECT * FROM model_runs WHERE 1 = 1"
        params: list[Any] = []
        if model_type is not None:
            sql += " AND model_type = ?"
            params.append(ModelType(model_type).value)
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        return [ModelRunRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def set_status(self, run_id: str, status: ModelRunStatus, approved_by: str | None = None, *,
                   autonomy: AutonomyLevel | None = None) -> None:
        """``autonomy`` is the level in force for a promotion: PAPER_READY needs
        a PAPER-promotion level, LIVE_CANDIDATE / LIVE_APPROVED need
        LIVE_CANDIDATE_APPROVAL (also enforced by schema triggers)."""
        status = ModelRunStatus(status)
        level = AutonomyLevel(autonomy) if autonomy is not None else None
        if status is ModelRunStatus.LIVE_APPROVED and not approved_by:
            raise ValueError("LIVE_APPROVED requires a named human approver")
        if status in REAL_DATA_ONLY_STATUSES:
            current = self.get(run_id)
            if current is not None and current.data_source is not ModelDataSource.HISTORICAL:
                raise ValueError(f"{status.value} requires a model trained on HISTORICAL data "
                                 f"(this one: {current.data_source.value})")
        if status is ModelRunStatus.PAPER_READY and level not in PAPER_PROMOTION_LEVELS:
            raise ValueError(f"PAPER_READY is not allowed at autonomy level {level.value if level else None}")
        if (status in (ModelRunStatus.LIVE_CANDIDATE, ModelRunStatus.LIVE_APPROVED)
                and level is not AutonomyLevel.LIVE_CANDIDATE_APPROVAL):
            raise ValueError(f"{status.value} is not allowed at autonomy level {level.value if level else None}")
        cur = self.db.execute(
            "UPDATE model_runs SET status = ?, approved_by = COALESCE(?, approved_by), "
            "promoted_autonomy = COALESCE(?, promoted_autonomy), updated_at = ? WHERE run_id = ?",
            (status.value, approved_by, level.value if level else None, self.now().isoformat(), run_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"model run {run_id!r} not found")


class LearningEventRepository(_Clocked):
    def insert(self, event: LearningEventRecord) -> int:
        return _insert(self.db, "learning_events", _stamp(event, self.clock).to_row(exclude={"id"}))

    def list_for_strategy(self, strategy_id: str) -> list[LearningEventRecord]:
        rows = self.db.query(
            "SELECT * FROM learning_events WHERE strategy_id = ? ORDER BY created_at, id", (strategy_id,)
        )
        return [LearningEventRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# v5: RL decisions, trade journal (mode-scoped), strategy versions and
# deployments (research / promotion artefacts, PAPER-only deployments)
# --------------------------------------------------------------------------- #


class RLDecisionRepository(_ModeScoped):
    """Append-only RL decision log."""

    def insert(self, decision: RLDecisionRecord) -> int:
        self._guard(decision)
        return _insert(self.db, "rl_decisions", _stamp(decision, self.clock).to_row(exclude={"id"}))

    def list_for_strategy(self, strategy_id: str, *, since: datetime | None = None,
                          until: datetime | None = None) -> list[RLDecisionRecord]:
        sql = "SELECT * FROM rl_decisions WHERE mode = ? AND strategy_id = ?"
        params: list[Any] = [self.mode.value, strategy_id]
        if since is not None:
            sql += " AND decided_at >= ?"
            params.append(ensure_ist(since).isoformat())
        if until is not None:
            sql += " AND decided_at < ?"
            params.append(ensure_ist(until).isoformat())
        return [RLDecisionRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def list_for_trade(self, trade_uid: str) -> list[RLDecisionRecord]:
        rows = self.db.query("SELECT * FROM rl_decisions WHERE mode = ? AND trade_uid = ? ORDER BY id",
                             (self.mode.value, trade_uid))
        return [RLDecisionRecord.from_row(r) for r in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM rl_decisions WHERE mode = ?", (self.mode.value,))
        return int(row["n"]) if row else 0


class TradeJournalRepository(_ModeScoped):
    """Append-only trade journal (one row per closed trade)."""

    def insert(self, entry: TradeJournalRecord) -> bool:
        """Insert unless the trade is already journaled. True if inserted."""
        self._guard(entry)
        if self.get(entry.trade_uid) is not None:
            return False
        _insert(self.db, "trade_journal", _stamp(entry, self.clock).to_row(exclude={"id"}))
        return True

    def get(self, trade_uid: str) -> TradeJournalRecord | None:
        row = self.db.query_one("SELECT * FROM trade_journal WHERE mode = ? AND trade_uid = ?",
                                (self.mode.value, trade_uid))
        return TradeJournalRecord.from_row(row) if row else None

    def list(self, strategy_id: str | None = None, *, since: datetime | None = None,
             until: datetime | None = None) -> list[TradeJournalRecord]:
        """Entries by exit time, oldest first; ``until`` is exclusive."""
        sql = "SELECT * FROM trade_journal WHERE mode = ?"
        params: list[Any] = [self.mode.value]
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        if since is not None:
            sql += " AND exit_time >= ?"
            params.append(ensure_ist(since).isoformat())
        if until is not None:
            sql += " AND exit_time < ?"
            params.append(ensure_ist(until).isoformat())
        return [TradeJournalRecord.from_row(r) for r in self.db.query(sql + " ORDER BY exit_time, id", params)]

    def journaled_uids(self) -> set[str]:
        return {r["trade_uid"] for r in self.db.query("SELECT trade_uid FROM trade_journal WHERE mode = ?",
                                                      (self.mode.value,))}


class StrategyVersionRepository(_Clocked):
    def insert(self, version: StrategyVersionRecord) -> int:
        if version.status not in (StrategyVersionStatus.BASELINE, StrategyVersionStatus.CANDIDATE):
            raise ValueError("new strategy versions start as BASELINE or CANDIDATE")
        return _insert(self.db, "strategy_versions", _stamp(version, self.clock).to_row(exclude={"id"}))

    def get(self, version_uid: str) -> StrategyVersionRecord | None:
        row = self.db.query_one("SELECT * FROM strategy_versions WHERE version_uid = ?", (version_uid,))
        return StrategyVersionRecord.from_row(row) if row else None

    def find(self, strategy_id: str, version: str) -> StrategyVersionRecord | None:
        row = self.db.query_one("SELECT * FROM strategy_versions WHERE strategy_id = ? AND version = ?",
                                (strategy_id, version))
        return StrategyVersionRecord.from_row(row) if row else None

    def list(self, strategy_id: str | None = None,
             statuses: Iterable[StrategyVersionStatus] | None = None) -> list[StrategyVersionRecord]:
        sql = "SELECT * FROM strategy_versions WHERE 1 = 1"
        params: list[Any] = []
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        wanted = [StrategyVersionStatus(x).value for x in statuses] if statuses is not None else None
        if wanted is not None:
            if not wanted:
                return []
            sql += f" AND status IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        return [StrategyVersionRecord.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def versions(self, strategy_id: str) -> Sequence[str]:
        return [r["version"] for r in self.db.query("SELECT version FROM strategy_versions WHERE strategy_id = ?",
                                                    (strategy_id,))]

    def set_status(self, version_uid: str, status: StrategyVersionStatus, *, reason: str,
                   autonomy: AutonomyLevel | None = None, approved_by: str | None = None,
                   evaluation: dict[str, Any] | None = None, evidence_source: EvidenceSource | None = None) -> None:
        """Move a version to ``status``. The schema triggers enforce the allowed
        transitions, HISTORICAL evidence for PAPER_READY and beyond, and the
        autonomy level; the same rules are checked here first."""
        status = StrategyVersionStatus(status)
        current = self.get(version_uid)
        if current is None:
            raise NotFoundError(f"strategy version {version_uid!r} not found")
        evidence = EvidenceSource(evidence_source) if evidence_source is not None else current.evidence_source
        if status in REAL_EVIDENCE_VERSION_STATUSES and evidence is not EvidenceSource.HISTORICAL:
            raise ValueError(f"{status.value} requires verification on HISTORICAL data (this one: {evidence.value})")
        if status is StrategyVersionStatus.LIVE_APPROVED and not (approved_by or current.approved_by or "").strip():
            raise ValueError("LIVE_APPROVED requires a named human approver")
        fields: dict[str, Any] = {"status": status.value, "status_reason": reason[:2000],
                                  "updated_at": self.now().isoformat()}
        if autonomy is not None:
            fields["autonomy_level"] = AutonomyLevel(autonomy).value
        if approved_by is not None:
            fields["approved_by"] = approved_by.strip()
        if evaluation is not None:
            fields["evaluation_json"] = json.dumps(evaluation, sort_keys=True, default=str)
        if evidence_source is not None:
            fields["evidence_source"] = evidence.value
        sets = ", ".join(f"{_ident(k)} = ?" for k in fields)
        self.db.execute(f"UPDATE strategy_versions SET {sets} WHERE version_uid = ?",  # noqa: S608
                        (*fields.values(), version_uid))


class StrategyDeploymentRepository(_Clocked):
    """PAPER deployment history; the newest row per strategy is in force."""

    def insert(self, deployment: StrategyDeploymentRecord) -> int:
        if deployment.mode is not TradingMode.PAPER:
            raise ValueError("only PAPER deployments can be recorded")
        return _insert(self.db, "strategy_deployments", _stamp(deployment, self.clock).to_row(exclude={"id"}))

    def current(self, strategy_id: str) -> StrategyDeploymentRecord | None:
        row = self.db.query_one("SELECT * FROM strategy_deployments WHERE mode = 'PAPER' AND strategy_id = ? "
                                "ORDER BY id DESC LIMIT 1", (strategy_id,))
        return StrategyDeploymentRecord.from_row(row) if row else None

    def history(self, strategy_id: str) -> list[StrategyDeploymentRecord]:
        rows = self.db.query("SELECT * FROM strategy_deployments WHERE mode = 'PAPER' AND strategy_id = ? ORDER BY id",
                             (strategy_id,))
        return [StrategyDeploymentRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #


class ControlCommandRepository(_ModeScoped):
    """Dashboard -> bot command queue (oldest first)."""

    def enqueue(self, cmd: ControlCommandRecord) -> int:
        self._guard(cmd)
        if cmd.status is not ControlCommandStatus.PENDING:
            raise ValueError("new commands must be PENDING")
        return _insert(self.db, "control_commands", _stamp(cmd, self.clock).to_row(exclude={"id"}))

    def get(self, command_id: int) -> ControlCommandRecord | None:
        row = self.db.query_one("SELECT * FROM control_commands WHERE mode = ? AND id = ?",
                                (self.mode.value, int(command_id)))
        return ControlCommandRecord.from_row(row) if row else None

    def pending(self, limit: int = 50) -> list[ControlCommandRecord]:
        rows = self.db.query("SELECT * FROM control_commands WHERE mode = ? AND status = 'PENDING' ORDER BY id LIMIT ?",
                             (self.mode.value, int(limit)))
        return [ControlCommandRecord.from_row(r) for r in rows]

    def recent(self, limit: int = 50) -> list[ControlCommandRecord]:
        rows = self.db.query("SELECT * FROM control_commands WHERE mode = ? ORDER BY id DESC LIMIT ?",
                             (self.mode.value, int(limit)))
        return [ControlCommandRecord.from_row(r) for r in rows]

    def last_completed(self, command: ControlCommandType,
                       status: ControlCommandStatus = ControlCommandStatus.DONE) -> ControlCommandRecord | None:
        """The most recently processed command of this type with this status."""
        row = self.db.query_one(
            "SELECT * FROM control_commands WHERE mode = ? AND command = ? AND status = ? "
            "ORDER BY processed_at DESC, id DESC LIMIT 1",
            (self.mode.value, ControlCommandType(command).value, ControlCommandStatus(status).value))
        return ControlCommandRecord.from_row(row) if row else None

    def complete(self, command_id: int, status: ControlCommandStatus, message: str, *, at: datetime,
                 result: dict[str, Any] | None = None) -> bool:
        """Finalise a PENDING command. Returns False if it was already processed."""
        status = ControlCommandStatus(status)
        if status is ControlCommandStatus.PENDING:
            raise ValueError("cannot complete a command as PENDING")
        cur = self.db.execute(
            "UPDATE control_commands SET status = ?, result_message = ?, result_json = ?, processed_at = ? "
            "WHERE mode = ? AND id = ? AND status = 'PENDING'",
            (status.value, message[:2000], json.dumps(result or {}, default=str), at.isoformat(), self.mode.value,
             int(command_id)),
        )
        return int(cur.rowcount) == 1


class RuntimeStateRepository(_ModeScoped):
    """The bot's latest published state snapshot (one row per mode)."""

    def publish(self, state: dict[str, Any], *, at: datetime) -> None:
        self.db.execute(
            "INSERT INTO runtime_state(mode, state_json, published_at) VALUES (?, ?, ?) "
            "ON CONFLICT(mode) DO UPDATE SET state_json = excluded.state_json, published_at = excluded.published_at",
            (self.mode.value, json.dumps(state, default=str), at.isoformat()),
        )

    def latest(self) -> tuple[dict[str, Any], datetime] | None:
        row = self.db.query_one("SELECT state_json, published_at FROM runtime_state WHERE mode = ?",
                                (self.mode.value,))
        if row is None:
            return None
        return json.loads(row["state_json"]), ensure_ist(datetime.fromisoformat(row["published_at"]))


class MarketHistoryRepository(_Clocked):
    """Recorded candles and option quotes, for reviewing a session afterwards.
    Market data is the same in every trading mode, so it is not mode-scoped.
    Nothing in trading reads these tables."""

    _CANDLE_COLUMNS = ("instrument_key", "timeframe_minutes", "start_time", "end_time", "open", "high", "low", "close",
                       "volume", "oi", "tick_count", "complete", "source", "recorded_at")

    def save_candles(self, rows: Iterable[tuple[Candle, str]]) -> int:
        """Upsert (candle, source) pairs. A complete candle replaces a partial
        one; a candle already recorded as complete is never rewritten."""
        at = self.now().isoformat()
        values = [(c.instrument_key, c.timeframe_minutes, c.start.isoformat(), c.end.isoformat(), c.open, c.high, c.low,
                   c.close, c.volume, c.oi, c.tick_count, int(c.complete), source, at) for c, source in rows]
        if not values:
            return 0
        columns = ", ".join(self._CANDLE_COLUMNS)
        marks = ", ".join("?" for _ in self._CANDLE_COLUMNS)
        keys = ("instrument_key", "timeframe_minutes", "start_time")
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._CANDLE_COLUMNS if c not in keys)
        sql = (f"INSERT INTO candles({columns}) VALUES ({marks}) "  # noqa: S608 - fixed column names
               f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates} "
               "WHERE candles.complete = 0 AND excluded.complete = 1")
        with self.db.transaction() as conn:
            conn.executemany(sql, values)
        return len(values)

    def candles(self, instrument_key: str, timeframe_minutes: int, day: date) -> list[Row]:
        return self.db.query(
            "SELECT * FROM candles WHERE instrument_key = ? AND timeframe_minutes = ? AND start_time >= ? "
            "AND start_time < ? ORDER BY start_time",
            (instrument_key, timeframe_minutes, day.isoformat(), (day + timedelta(days=1)).isoformat()))

    def save_quotes(self, rows: Iterable[tuple[Tick, bool]]) -> int:
        """Insert (tick, held) pairs."""
        values = [(t.instrument_key, t.exchange_time.isoformat(), t.received_at.isoformat(), t.ltp, t.bid, t.ask,
                   t.bid_qty, t.ask_qty, t.volume_today, t.oi, t.iv, int(held)) for t, held in rows]
        if not values:
            return 0
        with self.db.transaction() as conn:
            conn.executemany("INSERT INTO option_quotes(instrument_key, exchange_time, received_at, ltp, bid, ask, "
                             "bid_qty, ask_qty, volume_today, oi, iv, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
        return len(values)

    def purge_quotes(self, before: date) -> int:
        """Delete option quotes from before ``before``. Returns how many."""
        return int(self.db.execute("DELETE FROM option_quotes WHERE exchange_time < ?", (before.isoformat(),)).rowcount)

    def quotes(self, instrument_key: str) -> list[Row]:
        return self.db.query("SELECT * FROM option_quotes WHERE instrument_key = ? ORDER BY exchange_time, id",
                             (instrument_key,))


class Repositories:
    """All repositories for one trading mode, sharing one Database and one
    clock (the source of every timestamp they write)."""

    def __init__(self, db: Database, mode: TradingMode, *, clock: Clock = now_ist) -> None:
        self.db = db
        self.mode = TradingMode(mode)
        self.clock = clock
        self.strategies = StrategyRepository(db, clock=clock)
        self.orders = OrderRepository(db, self.mode, clock=clock)
        self.fills = FillRepository(db, self.mode, clock=clock)
        self.positions = PositionRepository(db, self.mode, clock=clock)
        self.trades = TradeRepository(db, self.mode, clock=clock)
        self.daily_pnl = DailyPnlRepository(db, self.mode, clock=clock)
        self.risk_events = RiskEventRepository(db, self.mode, clock=clock)
        self.feature_snapshots = FeatureSnapshotRepository(db, self.mode, clock=clock)
        self.model_runs = ModelRunRepository(db, clock=clock)
        self.learning_events = LearningEventRepository(db, clock=clock)
        self.commands = ControlCommandRepository(db, self.mode, clock=clock)
        self.runtime_state = RuntimeStateRepository(db, self.mode, clock=clock)
        self.rl_decisions = RLDecisionRepository(db, self.mode, clock=clock)
        self.journal = TradeJournalRepository(db, self.mode, clock=clock)
        self.strategy_versions = StrategyVersionRepository(db, clock=clock)
        self.deployments = StrategyDeploymentRepository(db, clock=clock)
        self.market_history = MarketHistoryRepository(db, clock=clock)

    def with_clock(self, clock: Clock) -> Repositories:
        """The same database and mode, stamping timestamps with ``clock``."""
        if clock is self.clock:
            return self
        return Repositories(self.db, self.mode, clock=clock)


__all__: Sequence[str] = (
    "DailyPnlRepository",
    "FeatureSnapshotRepository",
    "FillRepository",
    "LearningEventRepository",
    "MarketHistoryRepository",
    "ModeIsolationError",
    "ModelRunRepository",
    "NotFoundError",
    "OrderRepository",
    "PositionRepository",
    "Repositories",
    "RLDecisionRepository",
    "RiskEventRepository",
    "StrategyDeploymentRepository",
    "StrategyRepository",
    "StrategyVersionRepository",
    "TradeJournalRepository",
    "TradeRepository",
)
