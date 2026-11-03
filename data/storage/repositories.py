"""Data-access layer.

Mode-scoped repositories (orders, fills, positions, trades, daily P&L, risk
events, feature snapshots) are bound to ONE trading mode at construction.
Every read filters by that mode and every write is rejected if the record's
mode differs, so PAPER code cannot read or write LIVE rows and vice versa.
Strategy isolation is preserved by always scoping by ``strategy_id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, TypeVar

from core.clock import now_ist
from core.enums import (
    ModelRunStatus,
    ModelType,
    OrderStatus,
    PositionStatus,
    StrategyStatus,
    TradeStatus,
    TradingMode,
)
from data.storage.database import Database
from data.storage.records import (
    DailyPnlRecord,
    FeatureSnapshotRecord,
    FillRecord,
    LearningEventRecord,
    ModelRunRecord,
    OrderRecord,
    PositionRecord,
    Record,
    RiskEventRecord,
    StrategyRecord,
    TradeRecord,
)

R = TypeVar("R", bound=Record)

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


class _ModeScoped:
    def __init__(self, db: Database, mode: TradingMode) -> None:
        self.db = db
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


class StrategyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, record: StrategyRecord) -> None:
        _upsert(self.db, "strategies", record.to_row(), conflict=("strategy_id",), keep=("created_at",))

    def get(self, strategy_id: str) -> StrategyRecord | None:
        row = self.db.query_one("SELECT * FROM strategies WHERE strategy_id = ?", (strategy_id,))
        return StrategyRecord.from_row(row) if row else None

    def list_all(self) -> list[StrategyRecord]:
        return [StrategyRecord.from_row(r) for r in self.db.query("SELECT * FROM strategies ORDER BY strategy_id")]

    def set_status(self, strategy_id: str, status: StrategyStatus) -> None:
        cur = self.db.execute(
            "UPDATE strategies SET status = ?, updated_at = ? WHERE strategy_id = ?",
            (StrategyStatus(status).value, now_ist().isoformat(), strategy_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"strategy {strategy_id!r} not found")


# --------------------------------------------------------------------------- #
# orders / fills
# --------------------------------------------------------------------------- #


class OrderRepository(_ModeScoped):
    def insert(self, order: OrderRecord) -> int:
        self._guard(order)
        return _insert(self.db, "orders", order.to_row(exclude={"id"}))

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
        fields: dict[str, Any] = {"status": OrderStatus(status), "updated_at": now_ist()}
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
        return _insert(self.db, "fills", fill.to_row(exclude={"id"}))

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
        return _insert(self.db, "positions", position.to_row(exclude={"id"}))

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
        fields["updated_at"] = now_ist()
        if self._update("positions", {"id": position_id}, fields) == 0:
            raise NotFoundError(f"{self.mode.value} position {position_id} not found")

    def close(self, position_id: int, realized_pnl: float) -> None:
        self.update(
            position_id, quantity=0, status=PositionStatus.CLOSED, closed_at=now_ist(), realized_pnl=realized_pnl,
            unrealized_pnl=0.0,
        )


class TradeRepository(_ModeScoped):
    def insert(self, trade: TradeRecord) -> int:
        self._guard(trade)
        return _insert(self.db, "trades", trade.to_row(exclude={"id"}))

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

    def count_entries_on(self, strategy_id: str, day: date) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM trades WHERE mode = ? AND strategy_id = ? AND substr(entry_time, 1, 10) = ?",
            (self.mode.value, strategy_id, day.isoformat()),
        )
        return int(row["n"]) if row else 0

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
        now = now_ist()
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
        return _insert(self.db, "risk_events", event.to_row(exclude={"id"}))

    def recent(self, limit: int = 50, strategy_id: str | None = None) -> list[RiskEventRecord]:
        sql = "SELECT * FROM risk_events WHERE mode = ?"
        params: list[Any] = [self.mode.value]
        if strategy_id is not None:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [RiskEventRecord.from_row(r) for r in self.db.query(sql, params)]


class FeatureSnapshotRepository(_ModeScoped):
    def insert(self, snapshot: FeatureSnapshotRecord) -> int:
        self._guard(snapshot)
        return _insert(self.db, "feature_snapshots", snapshot.to_row(exclude={"id"}))

    def list_for_trade(self, trade_uid: str) -> list[FeatureSnapshotRecord]:
        rows = self.db.query(
            "SELECT * FROM feature_snapshots WHERE mode = ? AND trade_uid = ? ORDER BY snapshot_time, id",
            (self.mode.value, trade_uid),
        )
        return [FeatureSnapshotRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# model runs / learning events (not mode-scoped: research artefacts)
# --------------------------------------------------------------------------- #


class ModelRunRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, run: ModelRunRecord) -> int:
        return _insert(self.db, "model_runs", run.to_row(exclude={"id"}))

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

    def set_status(self, run_id: str, status: ModelRunStatus, approved_by: str | None = None) -> None:
        status = ModelRunStatus(status)
        if status is ModelRunStatus.LIVE_APPROVED and not approved_by:
            raise ValueError("LIVE_APPROVED requires a named human approver")
        cur = self.db.execute(
            "UPDATE model_runs SET status = ?, approved_by = COALESCE(?, approved_by), updated_at = ? WHERE run_id = ?",
            (status.value, approved_by, now_ist().isoformat(), run_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"model run {run_id!r} not found")


class LearningEventRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, event: LearningEventRecord) -> int:
        return _insert(self.db, "learning_events", event.to_row(exclude={"id"}))

    def list_for_strategy(self, strategy_id: str) -> list[LearningEventRecord]:
        rows = self.db.query(
            "SELECT * FROM learning_events WHERE strategy_id = ? ORDER BY created_at, id", (strategy_id,)
        )
        return [LearningEventRecord.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #


class Repositories:
    """All repositories for one trading mode, sharing one Database."""

    def __init__(self, db: Database, mode: TradingMode) -> None:
        self.db = db
        self.mode = TradingMode(mode)
        self.strategies = StrategyRepository(db)
        self.orders = OrderRepository(db, self.mode)
        self.fills = FillRepository(db, self.mode)
        self.positions = PositionRepository(db, self.mode)
        self.trades = TradeRepository(db, self.mode)
        self.daily_pnl = DailyPnlRepository(db, self.mode)
        self.risk_events = RiskEventRepository(db, self.mode)
        self.feature_snapshots = FeatureSnapshotRepository(db, self.mode)
        self.model_runs = ModelRunRepository(db)
        self.learning_events = LearningEventRepository(db)


__all__: Sequence[str] = (
    "DailyPnlRepository",
    "FeatureSnapshotRepository",
    "FillRepository",
    "LearningEventRepository",
    "ModeIsolationError",
    "ModelRunRepository",
    "NotFoundError",
    "OrderRepository",
    "PositionRepository",
    "Repositories",
    "RiskEventRepository",
    "StrategyRepository",
    "TradeRepository",
)
