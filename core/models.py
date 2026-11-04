"""Broker-agnostic domain models shared by data, broker, execution and strategies.

Strategies depend on these (never on ``broker/``), so PAPER, LIVE and backtest
feeds are interchangeable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from core.clock import ensure_ist, now_ist
from core.enums import OptionType, OrderSide, OrderStatus, OrderType, ProductType, TradingMode

ISTDatetime = Annotated[datetime, AfterValidator(ensure_ist)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class Greeks(_Model):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


class Tick(_Model):
    """One market-data update for one instrument.

    ``exchange_time`` (last trade time) is used for candle bucketing, never the
    local receipt time, so candles are point-in-time correct.
    """

    instrument_key: str
    ltp: float = Field(ge=0)
    exchange_time: ISTDatetime
    received_at: ISTDatetime = Field(default_factory=now_ist)
    ltq: int = Field(default=0, ge=0)
    close_price: float | None = None
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    bid_qty: int | None = Field(default=None, ge=0)
    ask_qty: int | None = Field(default=None, ge=0)
    volume_today: int | None = Field(default=None, ge=0)  # cumulative day volume (vtt)
    oi: float | None = Field(default=None, ge=0)
    iv: float | None = None
    atp: float | None = None
    greeks: Greeks | None = None
    is_snapshot: bool = False  # initial snapshot / REST recovery rather than live update

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return self.ask - self.bid

    @property
    def mid(self) -> float | None:
        if self.spread is None:
            return None
        assert self.bid is not None and self.ask is not None
        return (self.bid + self.ask) / 2


class Candle(_Model):
    instrument_key: str
    timeframe_minutes: int = Field(gt=0)
    start: ISTDatetime
    end: ISTDatetime
    open: float
    high: float
    low: float
    close: float
    volume: int = Field(default=0, ge=0)
    oi: float | None = None
    tick_count: int = Field(default=0, ge=0)
    complete: bool = True

    @model_validator(mode="after")
    def _ohlc(self) -> Candle:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("inconsistent OHLC")
        if self.end <= self.start:
            raise ValueError("candle end must be after start")
        return self


class Instrument(_Model):
    """A tradable contract (from the Upstox option-contract / instrument API)."""

    instrument_key: str
    trading_symbol: str
    segment: str
    underlying_key: str | None = None
    underlying_symbol: str | None = None
    instrument_type: str  # CE / PE / FUT / INDEX / EQ ...
    expiry: date | None = None
    strike: float | None = None
    lot_size: int = Field(ge=1)
    tick_size: float = Field(gt=0)
    freeze_quantity: int | None = None
    weekly: bool | None = None

    @property
    def option_type(self) -> OptionType | None:
        return OptionType(self.instrument_type) if self.instrument_type in ("CE", "PE") else None


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #


class Signal(_Model):
    """A strategy's request to enter. It names a direction, not a contract or
    size: contract selection, ML filtering, risk and sizing happen downstream."""

    signal_id: str
    strategy_id: str
    strategy_version: str
    option_type: OptionType  # CE = bullish, PE = bearish (long options only)
    generated_at: ISTDatetime
    candle_end: ISTDatetime  # as-of time of the data used (point-in-time)
    underlying_key: str
    underlying_price: float
    reason: str
    features: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Orders / account
# --------------------------------------------------------------------------- #


class OrderRequest(_Model):
    client_order_id: str = Field(min_length=1)
    mode: TradingMode
    strategy_id: str
    instrument_key: str
    side: OrderSide
    quantity: int = Field(gt=0)
    order_type: OrderType
    product: ProductType = ProductType.INTRADAY
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    # PAPER only: simulated order id assigned by the order manager before
    # submission (so the CREATED row can be persisted first).
    paper_order_id: str | None = None

    @model_validator(mode="after")
    def _prices(self) -> OrderRequest:
        if not self.client_order_id.startswith(f"{self.mode.value}-"):
            raise ValueError("client_order_id prefix must match mode")
        if self.mode is TradingMode.LIVE and self.paper_order_id is not None:
            raise ValueError("LIVE orders must not carry a paper_order_id")
        needs_price = self.order_type in (OrderType.LIMIT, OrderType.SL)
        needs_trigger = self.order_type in (OrderType.SL, OrderType.SL_M)
        if needs_price and self.price is None:
            raise ValueError(f"{self.order_type} order requires price")
        if not needs_price and self.price is not None:
            raise ValueError(f"{self.order_type} order must not set price")
        if needs_trigger and self.trigger_price is None:
            raise ValueError(f"{self.order_type} order requires trigger_price")
        if not needs_trigger and self.trigger_price is not None:
            raise ValueError(f"{self.order_type} order must not set trigger_price")
        return self


class OrderAck(_Model):
    client_order_id: str
    broker_order_id: str | None = None  # LIVE
    paper_order_id: str | None = None  # PAPER
    status: OrderStatus
    message: str | None = None


class OrderUpdate(_Model):
    """Order state change, from the portfolio stream (LIVE) or PaperBroker."""

    client_order_id: str | None = None
    broker_order_id: str | None = None
    paper_order_id: str | None = None
    instrument_key: str
    side: OrderSide
    status: OrderStatus
    quantity: int = Field(ge=0)
    filled_quantity: int = Field(ge=0)
    average_price: float | None = None
    last_fill_quantity: int = Field(default=0, ge=0)
    last_fill_price: float | None = None
    last_fill_charges: float = Field(default=0.0, ge=0)
    last_fill_slippage: float = 0.0
    last_fill_id: str | None = None
    message: str | None = None
    raw_status: str | None = None
    timestamp: ISTDatetime = Field(default_factory=now_ist)


class BrokerPosition(_Model):
    instrument_key: str
    trading_symbol: str | None = None
    product: str
    quantity: int
    average_price: float
    last_price: float | None = None
    realised: float = 0.0
    unrealised: float = 0.0
    buy_quantity: int = 0
    sell_quantity: int = 0


class Funds(_Model):
    available_margin: float
    used_margin: float
    payin_amount: float = 0.0
    notional_cash: float = 0.0
