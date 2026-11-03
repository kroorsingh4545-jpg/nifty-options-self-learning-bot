"""Indian F&O (options) transaction charges.

Per executed order / fill on premium turnover (``quantity * price``):

* Brokerage         flat ₹ per executed order (charged once per order)
* STT               % of premium, SELL side only
* Exchange txn      % of premium, both sides
* SEBI fee          ₹ per crore of turnover, both sides
* Stamp duty        % of premium, BUY side only
* GST               % of (brokerage + exchange txn + SEBI fee)

Rates come from ``config/settings.yaml`` (``costs``). Shared with the
backtester in Phase 6.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import CostsSection
from core.enums import OrderSide

CRORE = 10_000_000.0


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.gst, 2)


class ChargeCalculator:
    def __init__(self, costs: CostsSection | None = None) -> None:
        self.costs = costs or CostsSection()

    def compute(
        self, side: OrderSide, quantity: int, price: float, *, include_brokerage: bool = True
    ) -> ChargeBreakdown:
        if quantity <= 0 or price < 0:
            raise ValueError("quantity must be positive and price non-negative")
        c = self.costs
        turnover = quantity * price
        brokerage = c.brokerage_per_order if include_brokerage else 0.0
        stt = turnover * c.stt_sell_pct / 100 if side is OrderSide.SELL else 0.0
        exchange = turnover * c.exchange_txn_pct / 100
        sebi = turnover * c.sebi_fee_per_crore / CRORE
        stamp = turnover * c.stamp_duty_buy_pct / 100 if side is OrderSide.BUY else 0.0
        gst = (brokerage + exchange + sebi) * c.gst_pct / 100
        return ChargeBreakdown(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_txn=round(exchange, 2),
            sebi_fee=round(sebi, 2),
            stamp_duty=round(stamp, 2),
            gst=round(gst, 2),
        )

    def round_trip(self, quantity: int, entry: float, exit_: float) -> float:
        """Total charges for buying then selling ``quantity`` (two orders)."""
        return round(
            self.compute(OrderSide.BUY, quantity, entry).total + self.compute(OrderSide.SELL, quantity, exit_).total, 2
        )
