"""Paper broker: real market data in, simulated fills out.

No network calls of any kind — it is impossible for an order placed here to
reach Upstox. Fill model:

* MARKET  BUY fills at ask (fallback LTP), SELL at bid (fallback LTP), plus
          ``slippage_ticks`` adverse ticks, rounded to the tick size.
* LIMIT   fills only when marketable, never worse than the limit price.
* SL-M    triggers when LTP crosses the trigger (BUY: >=, SELL: <=), then
          fills as MARKET. SL triggers then behaves as LIMIT.
* Partial fills (optional) cap each fill at the top-of-book quantity; the
          remainder rests and fills on later ticks.
* Never fills against a missing or stale quote (``max_quote_age_seconds``);
          a MARKET order without a fresh quote is REJECTED.
* Validation: lot-size multiple, freeze quantity, sufficient funds, and no
          naked option selling (SELL may not exceed the long position).
* Charges: brokerage (once per order), STT, exchange, SEBI, stamp, GST.
* ``client_order_id`` is an idempotency key: re-sending returns the
          existing order instead of creating a duplicate.
* Restart: ``restore_book`` rebuilds orders, positions, cash and charges
          from the persisted PAPER orders and fills, the way a real broker
          keeps its book while the bot restarts. Without it, every PAPER
          restart with open positions would look like a reconciliation
          mismatch.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from broker.broker_interface import Broker, OrderUpdateListener
from broker.charges import ChargeCalculator
from broker.errors import BrokerError
from config.settings import CostsSection, PaperBrokerSection
from core.clock import now_ist
from core.enums import OrderSide, OrderStatus, OrderType, TradingMode
from core.ids import new_paper_fill_id, new_paper_order_id
from core.models import BrokerPosition, Funds, Instrument, OrderAck, OrderRequest, OrderUpdate, Tick
from monitoring.json_logger import get_logger

log = get_logger(__name__)

TERMINAL = frozenset({OrderStatus.COMPLETE, OrderStatus.REJECTED, OrderStatus.CANCELLED})

QuoteLookup = Callable[[str], Tick | None]


@dataclass
class _PaperOrder:
    request: OrderRequest
    paper_order_id: str
    status: OrderStatus = OrderStatus.OPEN
    quantity: int = 0
    price: float | None = None
    trigger_price: float | None = None
    filled_quantity: int = 0
    average_price: float | None = None
    charges: float = 0.0
    triggered: bool = False
    message: str | None = None
    fills: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass
class _Position:
    instrument_key: str
    trading_symbol: str
    quantity: int = 0
    average_price: float = 0.0
    realised: float = 0.0
    buy_quantity: int = 0
    sell_quantity: int = 0


@dataclass(frozen=True)
class BookEntry:
    """One persisted PAPER order and its fills, for ``restore_book``."""

    request: OrderRequest
    status: OrderStatus
    trading_symbol: str
    fills: Sequence[tuple[int, float, float]] = ()  # (quantity, price, charges), oldest first


def _round_to_tick(price: float, tick: float, side: OrderSide) -> float:
    """Round adversely: up for BUY, down for SELL."""
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if side is OrderSide.BUY else math.floor(steps + 1e-9)
    return round(max(tick, steps * tick), 2)


class PaperBroker(Broker):
    mode = TradingMode.PAPER

    def __init__(
        self,
        *,
        quotes: QuoteLookup,
        settings: PaperBrokerSection | None = None,
        costs: CostsSection | None = None,
        instruments: Iterable[Instrument] = (),
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.settings = settings or PaperBrokerSection()
        self._quotes = quotes
        self._charges = ChargeCalculator(costs)
        self._now = now_fn
        self._instruments: dict[str, Instrument] = {i.instrument_key: i for i in instruments}
        self._orders: dict[str, _PaperOrder] = {}
        self._positions: dict[str, _Position] = {}
        self._cash = float(self.settings.starting_funds)
        self._charges_paid = 0.0
        self._listeners: list[OrderUpdateListener] = []

    # ------------------------------------------------------------------ #
    def restore_book(self, entries: Iterable[BookEntry]) -> dict[str, int]:
        """Rebuild the simulated book after a restart. Only allowed while the
        book is empty. Working orders (OPEN / PARTIALLY_FILLED) rest again and
        can fill on later ticks; CREATED / UNKNOWN orders never reached the
        book and are skipped. Returns the restored net positions."""
        if self._orders:
            raise BrokerError("paper book is not empty; restore_book is only allowed at startup")
        working = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMITTED)
        restored = 0
        for e in entries:
            if e.status not in TERMINAL and e.status not in working:
                continue
            req = e.request
            order = _PaperOrder(request=req, paper_order_id=req.paper_order_id or new_paper_order_id(),
                                quantity=req.quantity, price=req.price, trigger_price=req.trigger_price)
            for qty, price, charges in e.fills:
                self._book_fill(order, qty, price, charges, e.trading_symbol)
            if e.status in TERMINAL:
                order.status = e.status
            else:
                order.status = OrderStatus.PARTIALLY_FILLED if order.filled_quantity else OrderStatus.OPEN
                order.triggered = order.filled_quantity > 0
            self._orders[req.client_order_id] = order
            restored += 1
        net = {k: p.quantity for k, p in self._positions.items() if p.quantity}
        log.warning("paper book restored", extra={"orders": restored, "positions": net,
                                                  "cash": round(self._cash, 2)})
        return net

    def register_instruments(self, instruments: Iterable[Instrument]) -> None:
        for inst in instruments:
            self._instruments[inst.instrument_key] = inst

    def add_order_listener(self, listener: OrderUpdateListener) -> None:
        self._listeners.append(listener)

    def _emit(self, order: _PaperOrder, **fill: Any) -> OrderUpdate:
        update = OrderUpdate(
            client_order_id=order.request.client_order_id,
            paper_order_id=order.paper_order_id,
            instrument_key=order.request.instrument_key,
            side=order.request.side,
            status=order.status,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            average_price=order.average_price,
            message=order.message,
            timestamp=self._now(),
            **fill,
        )
        for fn in list(self._listeners):
            try:
                fn(update)
            except Exception:
                log.exception("paper order listener failed", extra={"order_id": order.request.client_order_id})
        return update

    def _ack(self, order: _PaperOrder) -> OrderAck:
        return OrderAck(
            client_order_id=order.request.client_order_id,
            paper_order_id=order.paper_order_id,
            status=order.status,
            message=order.message,
        )

    def _reject(self, order: _PaperOrder, reason: str) -> OrderAck:
        order.status = OrderStatus.REJECTED
        order.message = reason
        log.warning(
            "paper order rejected",
            extra={"order_id": order.request.client_order_id, "strategy_id": order.request.strategy_id,
                   "reason": reason},
        )
        self._emit(order)
        return self._ack(order)

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #
    def _fresh_quote(self, instrument_key: str) -> Tick | None:
        tick = self._quotes(instrument_key)
        if tick is None:
            return None
        age = (self._now() - tick.received_at).total_seconds()
        if age > self.settings.max_quote_age_seconds or tick.is_snapshot:
            return None
        return tick

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def _pending_sell_qty(self, instrument_key: str, exclude: str | None = None) -> int:
        return sum(
            o.remaining
            for o in self._orders.values()
            if o.request.instrument_key == instrument_key
            and o.request.side is OrderSide.SELL
            and o.status not in TERMINAL
            and o.request.client_order_id != exclude
        )

    def _reserved_cash(self, exclude: str | None = None) -> float:
        total = 0.0
        for o in self._orders.values():
            if o.request.side is OrderSide.BUY and o.status not in TERMINAL and o.request.client_order_id != exclude:
                total += o.remaining * self._reference_price(o) * 1.01
        return total

    def _reference_price(self, order: _PaperOrder) -> float:
        if order.price is not None:
            return order.price
        tick = self._quotes(order.request.instrument_key)
        if tick is not None:
            return tick.ask or tick.ltp
        return order.trigger_price or 0.0

    def _validate(self, order: _PaperOrder) -> str | None:
        req = order.request
        inst = self._instruments.get(req.instrument_key)
        if inst is None:
            return "unknown instrument (lot size not loaded)"
        if order.quantity % inst.lot_size != 0:
            return f"quantity {order.quantity} is not a multiple of lot size {inst.lot_size}"
        if inst.freeze_quantity and order.quantity > inst.freeze_quantity:
            return f"quantity {order.quantity} exceeds freeze quantity {inst.freeze_quantity}"
        for p in (order.price, order.trigger_price):
            if p is not None and abs(round(p / inst.tick_size) * inst.tick_size - p) > 1e-6:
                return f"price {p} is not a multiple of tick size {inst.tick_size}"
        if req.side is OrderSide.SELL and not self.settings.allow_short_options:
            held = self._positions.get(req.instrument_key)
            long_qty = held.quantity if held else 0
            pending = self._pending_sell_qty(req.instrument_key, exclude=req.client_order_id)
            if order.remaining + pending > long_qty:
                return "sell exceeds long position (naked option selling is not allowed)"
        if req.side is OrderSide.BUY:
            ref = self._reference_price(order)
            if ref <= 0:
                return "no reference price available for margin check"
            needed = order.remaining * ref * 1.01 + self._charges.costs.brokerage_per_order * 2
            available = self._cash - self._reserved_cash(exclude=req.client_order_id)
            if needed > available:
                return f"insufficient funds: need ~{needed:,.2f}, available {available:,.2f}"
        return None

    # ------------------------------------------------------------------ #
    # Order API
    # ------------------------------------------------------------------ #
    async def place_order(self, request: OrderRequest) -> OrderAck:
        if request.mode is not TradingMode.PAPER:
            raise BrokerError("PaperBroker only accepts PAPER orders")
        existing = self._orders.get(request.client_order_id)
        if existing is not None:  # idempotent re-send
            return self._ack(existing)
        order = _PaperOrder(
            request=request,
            paper_order_id=request.paper_order_id or new_paper_order_id(),
            quantity=request.quantity,
            price=request.price,
            trigger_price=request.trigger_price,
        )
        self._orders[request.client_order_id] = order
        if request.order_type is OrderType.MARKET and self._fresh_quote(request.instrument_key) is None:
            return self._reject(order, "no fresh market data for instrument")
        reason = self._validate(order)
        if reason is not None:
            return self._reject(order, reason)
        log.info(
            "paper order accepted",
            extra={"order_id": request.client_order_id, "strategy_id": request.strategy_id,
                   "side": request.side.value, "qty": request.quantity, "type": request.order_type.value},
        )
        self._emit(order)
        self._try_fill(order)
        return self._ack(order)

    async def modify_order(
        self,
        client_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> OrderAck:
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {client_order_id}")
        if order.status in TERMINAL:
            return OrderAck(client_order_id=client_order_id, paper_order_id=order.paper_order_id,
                            status=order.status, message="order is not modifiable")
        old = (order.quantity, order.price, order.trigger_price)
        if quantity is not None:
            if quantity < order.filled_quantity or quantity <= 0:
                return OrderAck(client_order_id=client_order_id, paper_order_id=order.paper_order_id,
                                status=order.status, message="quantity below filled quantity")
            order.quantity = quantity
        if price is not None:
            order.price = price
        if trigger_price is not None:
            order.trigger_price = trigger_price
        reason = self._validate(order)
        if reason is not None:
            order.quantity, order.price, order.trigger_price = old
            return OrderAck(client_order_id=client_order_id, paper_order_id=order.paper_order_id,
                            status=order.status, message=f"modify rejected: {reason}")
        order.message = "modified"
        self._emit(order)
        self._try_fill(order)
        return self._ack(order)

    async def cancel_order(self, client_order_id: str) -> OrderAck:
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {client_order_id}")
        if order.status in TERMINAL:
            return OrderAck(client_order_id=client_order_id, paper_order_id=order.paper_order_id,
                            status=order.status, message="order is not cancellable")
        order.status = OrderStatus.CANCELLED
        order.message = "cancelled"
        self._emit(order)
        return self._ack(order)

    async def get_orders(self) -> list[OrderUpdate]:
        return [
            OrderUpdate(
                client_order_id=o.request.client_order_id, paper_order_id=o.paper_order_id,
                instrument_key=o.request.instrument_key, side=o.request.side, status=o.status,
                quantity=o.quantity, filled_quantity=o.filled_quantity, average_price=o.average_price,
                message=o.message, timestamp=self._now(),
            )
            for o in self._orders.values()
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        out = []
        for p in self._positions.values():
            tick = self._quotes(p.instrument_key)
            ltp = tick.ltp if tick else None
            unreal = (ltp - p.average_price) * p.quantity if ltp is not None and p.quantity else 0.0
            out.append(BrokerPosition(
                instrument_key=p.instrument_key, trading_symbol=p.trading_symbol, product="I",
                quantity=p.quantity, average_price=p.average_price, last_price=ltp, realised=round(p.realised, 2),
                unrealised=round(unreal, 2), buy_quantity=p.buy_quantity, sell_quantity=p.sell_quantity,
            ))
        return out

    async def get_funds(self) -> Funds:
        used = sum(p.quantity * p.average_price for p in self._positions.values())
        available = self._cash - self._reserved_cash()
        return Funds(available_margin=round(available, 2), used_margin=round(used, 2),
                     notional_cash=round(self._cash, 2))

    @property
    def charges_paid(self) -> float:
        return round(self._charges_paid, 2)

    # ------------------------------------------------------------------ #
    # Market data -> fills
    # ------------------------------------------------------------------ #
    def on_tick(self, tick: Tick) -> None:
        for order in list(self._orders.values()):
            if order.request.instrument_key == tick.instrument_key and order.status not in TERMINAL:
                self._try_fill(order)

    def on_ticks(self, ticks: list[Tick]) -> None:
        for t in ticks:
            self.on_tick(t)

    def _try_fill(self, order: _PaperOrder) -> None:
        if order.status in TERMINAL or order.remaining <= 0:
            return
        tick = self._fresh_quote(order.request.instrument_key)
        if tick is None:
            return
        req = order.request
        side = req.side
        effective_type = req.order_type
        if req.order_type in (OrderType.SL, OrderType.SL_M):
            if not order.triggered:
                assert order.trigger_price is not None
                hit = tick.ltp >= order.trigger_price if side is OrderSide.BUY else tick.ltp <= order.trigger_price
                if not hit:
                    return
                order.triggered = True
            effective_type = OrderType.MARKET if req.order_type is OrderType.SL_M else OrderType.LIMIT

        inst = self._instruments[req.instrument_key]
        if side is OrderSide.BUY:
            ref = tick.ask if tick.ask else tick.ltp
            depth = tick.ask_qty
        else:
            ref = tick.bid if tick.bid else tick.ltp
            depth = tick.bid_qty
        slip = self.settings.slippage_ticks * inst.tick_size
        raw = ref + slip if side is OrderSide.BUY else ref - slip
        price = _round_to_tick(raw, inst.tick_size, side)

        if effective_type is OrderType.LIMIT:
            assert order.price is not None
            marketable = ref <= order.price if side is OrderSide.BUY else ref >= order.price
            if not marketable:
                return
            price = min(price, order.price) if side is OrderSide.BUY else max(price, order.price)

        qty = order.remaining
        if self.settings.simulate_partial_fills and depth:
            lots = depth // inst.lot_size
            if lots <= 0:
                return
            qty = min(qty, lots * inst.lot_size)
        self._apply_fill(order, qty, price, slippage=round(abs(price - ref), 2))

    def _apply_fill(self, order: _PaperOrder, qty: int, price: float, slippage: float) -> None:
        req = order.request
        first_fill = order.filled_quantity == 0
        charges = self._charges.compute(req.side, qty, price, include_brokerage=first_fill).total
        fill_id = self._book_fill(order, qty, price, charges, self._instruments[req.instrument_key].trading_symbol)
        log.info(
            "paper fill",
            extra={"order_id": req.client_order_id, "strategy_id": req.strategy_id, "qty": qty, "price": price,
                   "charges": charges, "status": order.status.value},
        )
        self._emit(order, last_fill_quantity=qty, last_fill_price=price, last_fill_charges=charges,
                   last_fill_slippage=slippage, last_fill_id=fill_id)

    def _book_fill(self, order: _PaperOrder, qty: int, price: float, charges: float, trading_symbol: str) -> str:
        """Order, position, cash and charges bookkeeping for one fill (shared by
        live fills and ``restore_book``)."""
        req = order.request
        prev_value = (order.average_price or 0.0) * order.filled_quantity
        order.filled_quantity += qty
        order.average_price = round((prev_value + qty * price) / order.filled_quantity, 4)
        order.charges += charges
        order.status = OrderStatus.COMPLETE if order.remaining == 0 else OrderStatus.PARTIALLY_FILLED
        order.message = None
        fill_id = new_paper_fill_id()
        order.fills.append(fill_id)

        pos = self._positions.setdefault(req.instrument_key, _Position(req.instrument_key, trading_symbol))
        if req.side is OrderSide.BUY:
            total = pos.quantity * pos.average_price + qty * price
            pos.quantity += qty
            pos.average_price = total / pos.quantity
            pos.buy_quantity += qty
            self._cash -= qty * price + charges
        else:
            pos.realised += (price - pos.average_price) * qty
            pos.quantity -= qty
            pos.sell_quantity += qty
            if pos.quantity == 0:
                pos.average_price = 0.0
            self._cash += qty * price - charges
        self._charges_paid += charges
        return fill_id
