"""Backtest cost model: realistic Indian F&O costs plus spread and slippage.

The backtester fills orders with the real ``PaperBroker``, so the cost RATES
applied to every simulated fill are exactly the bot's configured ones:

* Brokerage, STT (sell side), exchange transaction charges, SEBI fee, stamp
  duty (buy side) and GST: ``broker.charges.ChargeCalculator`` with
  ``config/settings.yaml`` ``costs``. Brokerage is charged once per order (on
  its first fill), as the paper broker does.
* Slippage: ``paper_broker.slippage_ticks`` adverse ticks on every fill.
* Spread: historical candles carry no bid/ask, so this module builds a quote
  around each replayed price: ``half_spread = max(min_half_spread_ticks *
  tick, half_spread_pct% of price)``, bid rounded down, ask rounded up. BUYs
  pay the ask, SELLs receive the bid.
* Depth: top-of-book quantity of ``depth_lots`` lots per side; larger orders
  fill partially across replayed ticks (paper broker partial fills).

``attribute`` splits the costs of a run's fills into their components for the
report (cost impact), recomputing them with the same calculator.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from broker.charges import ChargeCalculator
from config.settings import CostsSection
from core.enums import OrderSide


class SpreadModel(BaseModel):
    """Synthetic top-of-book around a replayed option price."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    half_spread_pct: float = Field(default=0.25, ge=0, le=5)  # % of price, each side of the mid
    min_half_spread_ticks: int = Field(default=1, ge=1, le=20)
    depth_lots: int = Field(default=20, ge=1, le=1000)  # top-of-book quantity, in lots, per side


@dataclass(frozen=True)
class Execution:
    """One simulated fill, with the mid price at that moment."""

    strategy_id: str
    client_order_id: str
    side: OrderSide
    quantity: int
    price: float
    mid: float
    charges: float
    slippage_per_unit: float  # adverse ticks beyond the touch (paper broker)
    first_fill_of_order: bool


@dataclass(frozen=True)
class CostAttribution:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    slippage: float = 0.0  # rupees: adverse ticks x quantity
    spread: float = 0.0  # rupees: distance from the mid to the touch x quantity
    turnover: float = 0.0  # premium turnover (both sides)

    @property
    def charges(self) -> float:
        return round(self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.gst, 2)

    @property
    def execution(self) -> float:
        return round(self.slippage + self.spread, 2)

    @property
    def total(self) -> float:
        return round(self.charges + self.execution, 2)

    def as_dict(self) -> dict[str, float]:
        return {"brokerage": round(self.brokerage, 2), "stt": round(self.stt, 2),
                "exchange_txn": round(self.exchange_txn, 2), "sebi_fee": round(self.sebi_fee, 2),
                "stamp_duty": round(self.stamp_duty, 2), "gst": round(self.gst, 2), "charges": self.charges,
                "slippage": round(self.slippage, 2), "spread": round(self.spread, 2), "execution": self.execution,
                "total": self.total, "turnover": round(self.turnover, 2)}


class CostModel:
    def __init__(self, costs: CostsSection, *, slippage_ticks: int, spread: SpreadModel | None = None) -> None:
        self.costs = costs
        self.slippage_ticks = slippage_ticks
        self.spread = spread or SpreadModel()
        self.calculator = ChargeCalculator(costs)

    def quote(self, price: float, tick_size: float) -> tuple[float, float]:
        """(bid, ask) around ``price``: bid rounded down, ask up, bid >= 1 tick."""
        half = max(self.spread.min_half_spread_ticks * tick_size, price * self.spread.half_spread_pct / 100)
        bid = math.floor((price - half) / tick_size + 1e-9) * tick_size
        ask = math.ceil((price + half) / tick_size - 1e-9) * tick_size
        return round(max(tick_size, bid), 2), round(max(ask, tick_size * 2), 2)

    def depth(self, lot_size: int) -> int:
        return self.spread.depth_lots * lot_size

    def attribute(self, executions: Iterable[Execution]) -> CostAttribution:
        totals = dict.fromkeys(("brokerage", "stt", "exchange_txn", "sebi_fee", "stamp_duty", "gst", "slippage",
                                "spread", "turnover"), 0.0)
        for e in executions:
            b = self.calculator.compute(e.side, e.quantity, e.price, include_brokerage=e.first_fill_of_order)
            totals["brokerage"] += b.brokerage
            totals["stt"] += b.stt
            totals["exchange_txn"] += b.exchange_txn
            totals["sebi_fee"] += b.sebi_fee
            totals["stamp_duty"] += b.stamp_duty
            totals["gst"] += b.gst
            adverse = abs(e.price - e.mid) * e.quantity
            slip = min(adverse, e.slippage_per_unit * e.quantity)
            totals["slippage"] += slip
            totals["spread"] += adverse - slip
            totals["turnover"] += e.price * e.quantity
        return CostAttribution(**totals)

    def describe(self) -> dict[str, object]:
        return {"rates": self.costs.model_dump(), "slippage_ticks": self.slippage_ticks,
                "spread": self.spread.model_dump(),
                "brokerage_basis": "once per executed order (first fill)"}
