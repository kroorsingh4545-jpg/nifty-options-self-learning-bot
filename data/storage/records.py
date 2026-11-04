"""Pydantic record models for each persisted table.

These mirror the SQLite CHECK constraints (defence in depth): an invalid
record fails in Python before it reaches the database, and the database
rejects it again if Python is bypassed.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.clock import ensure_ist, now_ist
from core.enums import (
    REAL_DATA_ONLY_STATUSES,
    ControlCommandStatus,
    ControlCommandType,
    LearningStage,
    ModelDataSource,
    ModelRunStatus,
    ModelType,
    OptionType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    ProductType,
    RiskSeverity,
    SnapshotPurpose,
    StrategyStatus,
    TradeStatus,
    TradingMode,
)
from core.ids import STRATEGY_ID_PATTERN

ISTDatetime = Annotated[datetime, AfterValidator(ensure_ist)]
StrategyId = Annotated[str, Field(pattern=STRATEGY_ID_PATTERN)]


def _now() -> datetime:
    """Placeholder default for clock-stamped timestamp fields.

    Repositories replace every such field the caller left unset with THEIR
    injected clock on insert/upsert (``Record.clock_fields``), so a backtest
    running on simulated time never persists a wall-clock timestamp."""
    return now_ist()


class Record(BaseModel):
    """Base record. ``to_row`` / ``from_row`` convert to and from SQLite."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    #: Fields serialised as JSON text columns.
    json_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def clock_fields(cls) -> tuple[str, ...]:
        """Timestamp fields defaulting to "now": stamped by the repository clock."""
        return tuple(n for n, f in cls.model_fields.items() if f.default_factory is _now)

    @field_validator("*", mode="before")
    @classmethod
    def _decode_json(cls, value: Any, info: Any) -> Any:
        if info.field_name in cls.json_fields and isinstance(value, str):
            return json.loads(value)
        return value

    def to_row(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for name, value in self.model_dump(exclude=exclude or set()).items():
            if name in self.json_fields:
                row[name] = json.dumps(value, sort_keys=True, default=str)
            elif isinstance(value, date):  # also covers datetime
                row[name] = value.isoformat()
            elif isinstance(value, bool):
                row[name] = int(value)
            elif hasattr(value, "value"):  # enums
                row[name] = value.value
            else:
                row[name] = value
        return row

    @classmethod
    def from_row(cls, row: Any) -> Any:
        return cls.model_validate(dict(row))


def _check_mode_prefix(mode: TradingMode, identifier: str, field: str) -> None:
    if not identifier.startswith(f"{mode.value}-"):
        raise ValueError(f"{field} must start with '{mode.value}-' for a {mode.value} record")


# --------------------------------------------------------------------------- #


class StrategyRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = ("config_json",)

    strategy_id: StrategyId
    name: str
    version: str
    allocated_capital: float = Field(gt=0)
    enabled: bool = False
    status: StrategyStatus = StrategyStatus.DISABLED
    config_json: dict[str, Any] = Field(default_factory=dict)
    created_at: ISTDatetime = Field(default_factory=_now)
    updated_at: ISTDatetime = Field(default_factory=_now)


class OrderRecord(Record):
    id: int | None = None
    client_order_id: str
    mode: TradingMode
    paper_order_id: str | None = None
    broker_order_id: str | None = None
    strategy_id: StrategyId
    trade_uid: str | None = None
    instrument_key: str = Field(min_length=1)
    trading_symbol: str = Field(min_length=1)
    side: OrderSide
    order_type: OrderType
    product: ProductType = ProductType.INTRADAY
    purpose: OrderPurpose
    quantity: int = Field(gt=0)
    price: float | None = Field(default=None, ge=0)
    trigger_price: float | None = Field(default=None, ge=0)
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: int = Field(default=0, ge=0)
    average_price: float | None = Field(default=None, ge=0)
    status_message: str | None = None
    created_at: ISTDatetime = Field(default_factory=_now)
    updated_at: ISTDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _identifiers(self) -> OrderRecord:
        _check_mode_prefix(self.mode, self.client_order_id, "client_order_id")
        if self.trade_uid is not None:
            _check_mode_prefix(self.mode, self.trade_uid, "trade_uid")
        if self.mode is TradingMode.PAPER:
            if not self.paper_order_id:
                raise ValueError("PAPER orders require paper_order_id")
            if self.broker_order_id is not None:
                raise ValueError("PAPER orders must not carry a broker_order_id")
        elif self.paper_order_id is not None:
            raise ValueError("LIVE orders must not carry a paper_order_id")
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        return self


class FillRecord(Record):
    id: int | None = None
    order_id: int
    mode: TradingMode
    strategy_id: StrategyId
    paper_fill_id: str | None = None
    broker_fill_id: str | None = None
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    charges: float = Field(default=0.0, ge=0)
    slippage: float = 0.0
    filled_at: ISTDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _identifiers(self) -> FillRecord:
        if self.mode is TradingMode.PAPER:
            if not self.paper_fill_id or self.broker_fill_id is not None:
                raise ValueError("PAPER fills require paper_fill_id and no broker_fill_id")
        elif self.paper_fill_id is not None or not self.broker_fill_id:
            raise ValueError("LIVE fills require broker_fill_id and no paper_fill_id")
        return self


class PositionRecord(Record):
    id: int | None = None
    mode: TradingMode
    strategy_id: StrategyId
    instrument_key: str = Field(min_length=1)
    trading_symbol: str = Field(min_length=1)
    quantity: int
    average_price: float = Field(ge=0)
    last_price: float | None = Field(default=None, ge=0)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    stop_loss: float | None = Field(default=None, ge=0)
    target: float | None = Field(default=None, ge=0)
    trailing_stop: float | None = Field(default=None, ge=0)
    status: PositionStatus = PositionStatus.OPEN
    opened_at: ISTDatetime = Field(default_factory=_now)
    closed_at: ISTDatetime | None = None
    updated_at: ISTDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check(self) -> PositionRecord:
        if self.status is PositionStatus.OPEN and self.quantity != 0 and self.stop_loss is None:
            raise ValueError("an open position must have a stop_loss")
        if self.status is PositionStatus.CLOSED and (self.quantity != 0 or self.closed_at is None):
            raise ValueError("a closed position must be flat and have closed_at")
        return self


class TradeRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = ("trailing_config_json",)

    id: int | None = None
    trade_uid: str
    mode: TradingMode
    strategy_id: StrategyId
    strategy_version: str
    instrument_key: str = Field(min_length=1)
    trading_symbol: str = Field(min_length=1)
    option_type: OptionType
    strike: float = Field(gt=0)
    expiry: date
    side: OrderSide
    quantity: int = Field(gt=0)
    entry_price: float | None = Field(default=None, ge=0)
    entry_time: ISTDatetime | None = None
    exit_price: float | None = Field(default=None, ge=0)
    exit_time: ISTDatetime | None = None
    initial_stop_loss: float = Field(ge=0)
    stop_loss: float = Field(ge=0)
    target: float | None = Field(default=None, ge=0)
    trailing_config_json: dict[str, Any] = Field(default_factory=dict)
    entry_reason: str | None = None
    exit_reason: str | None = None
    gross_pnl: float | None = None
    charges: float = Field(default=0.0, ge=0)
    net_pnl: float | None = None
    r_multiple: float | None = None
    ml_confidence: float | None = Field(default=None, ge=0, le=1)
    regime: str | None = None
    status: TradeStatus = TradeStatus.OPEN
    created_at: ISTDatetime = Field(default_factory=_now)
    updated_at: ISTDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check(self) -> TradeRecord:
        _check_mode_prefix(self.mode, self.trade_uid, "trade_uid")
        if self.status is TradeStatus.CLOSED and (
            self.exit_price is None or self.exit_time is None or self.net_pnl is None
        ):
            raise ValueError("a CLOSED trade requires exit_price, exit_time and net_pnl")
        return self


class DailyPnlRecord(Record):
    trade_date: date
    mode: TradingMode
    strategy_id: StrategyId
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    charges: float = Field(default=0.0, ge=0)
    trades_count: int = Field(default=0, ge=0)
    wins: int = Field(default=0, ge=0)
    losses: int = Field(default=0, ge=0)
    consecutive_losses: int = Field(default=0, ge=0)
    max_drawdown: float = Field(default=0.0, ge=0)
    loss_limit_breached: bool = False
    updated_at: ISTDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check(self) -> DailyPnlRecord:
        if self.wins + self.losses > self.trades_count:
            raise ValueError("wins + losses cannot exceed trades_count")
        return self

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


class RiskEventRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = ("details_json",)

    id: int | None = None
    mode: TradingMode
    strategy_id: StrategyId | None = None  # None = global
    event_type: str = Field(min_length=1)
    severity: RiskSeverity
    message: str
    details_json: dict[str, Any] = Field(default_factory=dict)
    created_at: ISTDatetime = Field(default_factory=_now)


class FeatureSnapshotRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = ("features_json",)

    id: int | None = None
    mode: TradingMode
    strategy_id: StrategyId
    trade_uid: str | None = None
    instrument_key: str = Field(min_length=1)
    purpose: SnapshotPurpose
    snapshot_time: ISTDatetime
    feature_schema_version: str = Field(min_length=1)
    features_json: dict[str, Any]
    created_at: ISTDatetime = Field(default_factory=_now)


class ModelRunRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = (
        "feature_schema_json",
        "hyperparameters_json",
        "metrics_json",
        "seeds_json",
    )

    id: int | None = None
    run_id: str = Field(min_length=1)
    model_type: ModelType
    model_version: str = Field(min_length=1)
    strategy_id: StrategyId | None = None
    feature_schema_version: str = Field(min_length=1)
    feature_schema_json: dict[str, Any]
    training_start: ISTDatetime
    training_end: ISTDatetime
    hyperparameters_json: dict[str, Any] = Field(default_factory=dict)
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    seeds_json: list[int] = Field(default_factory=list)
    environment_version: str | None = None
    artifact_path: str = Field(min_length=1)
    status: ModelRunStatus = ModelRunStatus.TRAINED
    approved_by: str | None = None
    created_at: ISTDatetime = Field(default_factory=_now)
    updated_at: ISTDatetime = Field(default_factory=_now)
    data_source: ModelDataSource = ModelDataSource.UNKNOWN

    @model_validator(mode="after")
    def _check(self) -> ModelRunRecord:
        if self.status in REAL_DATA_ONLY_STATUSES and self.data_source is not ModelDataSource.HISTORICAL:
            raise ValueError(f"{self.status.value} requires a model trained on HISTORICAL data")
        if self.training_start >= self.training_end:
            raise ValueError("training_start must be before training_end")
        if self.status is ModelRunStatus.LIVE_APPROVED and not self.approved_by:
            raise ValueError("LIVE_APPROVED requires a named human approver")
        return self


class LearningEventRecord(Record):
    json_fields: ClassVar[tuple[str, ...]] = ("payload_json",)

    id: int | None = None
    event_uid: str = Field(min_length=1)
    strategy_id: StrategyId
    stage: LearningStage
    base_version: str | None = None
    candidate_version: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)
    outcome: str | None = None
    approved_by: str | None = None
    created_at: ISTDatetime = Field(default_factory=_now)


class ControlCommandRecord(Record):
    """An operator command queued by the dashboard. ``operator`` is the
    authenticated dashboard user; ``expires_at`` None never expires."""

    json_fields: ClassVar[tuple[str, ...]] = ("payload_json", "result_json")

    id: int | None = None
    mode: TradingMode
    command: ControlCommandType
    strategy_id: StrategyId | None = None
    target: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)
    operator: str = Field(min_length=1)
    note: str = ""
    status: ControlCommandStatus = ControlCommandStatus.PENDING
    result_message: str | None = None
    result_json: dict[str, Any] = Field(default_factory=dict)
    requested_at: ISTDatetime = Field(default_factory=_now)
    expires_at: ISTDatetime | None = None
    processed_at: ISTDatetime | None = None

    @field_validator("operator")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("operator must be a named person")
        return v.strip()
