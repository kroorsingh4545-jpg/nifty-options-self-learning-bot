"""Order manager — the ONLY component that calls the broker's order methods.

Order lifecycle (persisted in ``orders``; fills appended to ``fills``)::

    CREATED ──place──▶ SUBMITTED ──▶ OPEN ──▶ PARTIALLY_FILLED ──▶ COMPLETE
       │                  │            │              │
       │                  ├──▶ REJECTED├──▶ CANCELLED ┴──▶ CANCELLED (remainder)
       └──timeout────────▶ UNKNOWN ──(reconcile)──▶ any real state

* Transitions are validated; regressions (e.g. a late OPEN after COMPLETE)
  are ignored and logged, never applied.
* A timeout leaves the order UNKNOWN. It is reconciled with the broker before
  anything is re-sent — never blindly retried.
* ENTRY orders are refused while the kill switch blocks the strategy. Exit
  orders (EXIT / STOP_LOSS / TARGET / SQUARE_OFF / KILL_SWITCH) are always allowed.
* Rejections are logged as risk events and alerted; N consecutive rejections
  trigger the kill switch. Rejected entries are never retried automatically.
* ``exit_position`` retries safely: re-reads the remaining open quantity
  before every attempt, resolves UNKNOWN orders first, escalates to the kill
  switch + CRITICAL alert if the position cannot be closed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from broker.broker_interface import Broker
from broker.charges import ChargeCalculator
from broker.errors import BrokerError, BrokerTimeoutError, LiveTradingDisabledError
from broker.upstox_rate_limiter import backoff_delay
from config.settings import ExecutionSection
from core.clock import now_ist
from core.enums import OrderPurpose, OrderSide, OrderStatus, OrderType, RiskSeverity, TradingMode
from core.ids import new_client_order_id, new_paper_order_id
from core.models import Instrument, OrderRequest, OrderUpdate
from data.storage.records import FillRecord, OrderRecord, RiskEventRecord
from data.storage.repositories import Repositories
from monitoring.alerts import Alert, AlertSink, LogAlertSink
from monitoring.json_logger import get_logger
from risk.kill_switch import KillReason, KillSwitch

log = get_logger(__name__)

TERMINAL = frozenset({OrderStatus.COMPLETE, OrderStatus.REJECTED, OrderStatus.CANCELLED})
LIFECYCLE: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(set(OrderStatus) - {OrderStatus.CREATED}),
    OrderStatus.SUBMITTED: frozenset({OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.COMPLETE,
                                      OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.UNKNOWN}),
    OrderStatus.OPEN: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.COMPLETE, OrderStatus.CANCELLED,
                                 OrderStatus.REJECTED, OrderStatus.UNKNOWN}),
    OrderStatus.PARTIALLY_FILLED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.COMPLETE,
                                             OrderStatus.CANCELLED, OrderStatus.UNKNOWN}),
    OrderStatus.UNKNOWN: frozenset({OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED,
                                    OrderStatus.COMPLETE, OrderStatus.REJECTED, OrderStatus.CANCELLED}),
    OrderStatus.COMPLETE: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}
EXIT_PURPOSES = frozenset({OrderPurpose.EXIT, OrderPurpose.STOP_LOSS, OrderPurpose.TARGET, OrderPurpose.SQUARE_OFF,
                           OrderPurpose.KILL_SWITCH})


def transition_allowed(old: OrderStatus, new: OrderStatus) -> bool:
    return new in LIFECYCLE[old]


class EntryBlockedError(RuntimeError):
    """An ENTRY order was refused (kill switch / mode)."""


@dataclass(frozen=True)
class OrderIntent:
    strategy_id: str
    instrument: Instrument
    side: OrderSide
    quantity: int
    order_type: OrderType
    purpose: OrderPurpose
    trade_uid: str | None = None
    price: float | None = None
    trigger_price: float | None = None


OrderListener = Callable[[OrderRecord, OrderUpdate], None]


class OrderManager:
    def __init__(
        self,
        *,
        broker: Broker,
        repos: Repositories,
        kill_switch: KillSwitch,
        settings: ExecutionSection | None = None,
        max_consecutive_rejections: int = 3,
        alerts: AlertSink | None = None,
        now_fn: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        charges: ChargeCalculator | None = None,
    ) -> None:
        if broker.mode is not repos.mode:
            raise ValueError("broker and repositories must use the same trading mode")
        self.broker = broker
        self.repos = repos
        self.mode: TradingMode = repos.mode
        self.kill_switch = kill_switch
        self.settings = settings or ExecutionSection()
        self.max_rejections = max_consecutive_rejections
        self.alerts = alerts or LogAlertSink()
        self._now = now_fn
        self._sleep = sleep
        self._charges = charges or ChargeCalculator()
        self._listeners: list[OrderListener] = []
        self._events: dict[str, asyncio.Event] = {}
        self.rejection_streak = 0
        self.illegal_transitions = 0
        self.on_needs_reconcile: Callable[[str], None] | None = None
        broker.add_order_listener(self.on_broker_update)

    def add_listener(self, fn: OrderListener) -> None:
        self._listeners.append(fn)

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #
    async def submit(self, intent: OrderIntent) -> OrderRecord:
        sid = intent.strategy_id
        if intent.purpose is OrderPurpose.ENTRY:
            reason = self.kill_switch.block_reason(sid)
            if reason:
                raise EntryBlockedError(reason)
        coid = new_client_order_id(self.mode, sid)
        paper_id = new_paper_order_id() if self.mode is TradingMode.PAPER else None
        rec = OrderRecord(
            client_order_id=coid, mode=self.mode, paper_order_id=paper_id, strategy_id=sid,
            trade_uid=intent.trade_uid, instrument_key=intent.instrument.instrument_key,
            trading_symbol=intent.instrument.trading_symbol, side=intent.side, order_type=intent.order_type,
            purpose=intent.purpose, quantity=intent.quantity, price=intent.price, trigger_price=intent.trigger_price,
            created_at=self._now(), updated_at=self._now(),
        )
        self.repos.orders.insert(rec)  # CREATED is persisted before anything is sent
        self._events[coid] = asyncio.Event()
        request = OrderRequest(
            client_order_id=coid, mode=self.mode, strategy_id=sid, instrument_key=rec.instrument_key,
            side=intent.side, quantity=intent.quantity, order_type=intent.order_type, price=intent.price,
            trigger_price=intent.trigger_price, paper_order_id=paper_id,
        )
        log.info("submitting order", extra={"strategy_id": sid, "order_id": coid, "purpose": intent.purpose.value,
                                            "side": intent.side.value, "qty": intent.quantity,
                                            "type": intent.order_type.value})
        try:
            ack = await self.broker.place_order(request)
        except BrokerTimeoutError:
            current = self.get(coid)
            if current is not None and current.status in (OrderStatus.CREATED, OrderStatus.SUBMITTED):
                # Never regress an order the broker already reported progress on.
                self._set_status(coid, OrderStatus.UNKNOWN, "submission timed out; outcome unknown - reconcile")
            self._risk_event("ORDER_UNKNOWN", RiskSeverity.WARNING, f"order {coid} outcome unknown", sid)
            if self.on_needs_reconcile:
                self.on_needs_reconcile("order_timeout")
        except LiveTradingDisabledError as exc:
            self._set_status(coid, OrderStatus.REJECTED, str(exc))
            raise
        except BrokerError as exc:
            self._set_status(coid, OrderStatus.REJECTED, f"broker error: {exc}")
            self._on_rejection(coid, str(exc))
        else:
            current = self.get(coid)
            if current is not None and ack.status is not current.status and transition_allowed(current.status,
                                                                                                ack.status):
                self._set_status(coid, ack.status, ack.message)
                if ack.status is OrderStatus.REJECTED:
                    self._on_rejection(coid, ack.message or "rejected")
            if ack.status is not OrderStatus.REJECTED:
                self.rejection_streak = 0
        result = self.get(coid)
        assert result is not None
        return result

    def get(self, client_order_id: str) -> OrderRecord | None:
        return self.repos.orders.get(client_order_id)

    def _set_status(self, coid: str, status: OrderStatus, message: str | None = None) -> None:
        self.repos.orders.update_status(coid, status, status_message=message)
        if status in TERMINAL and coid in self._events:
            self._events[coid].set()

    # ------------------------------------------------------------------ #
    # Broker updates
    # ------------------------------------------------------------------ #
    def on_broker_update(self, update: OrderUpdate) -> None:
        coid = update.client_order_id
        rec = self.get(coid) if coid else None
        if rec is None:
            log.error("order update for unknown order", extra={"order_id": coid, "broker_id": update.broker_order_id})
            if self.on_needs_reconcile:
                self.on_needs_reconcile("unknown_order_update")
            return
        assert coid is not None
        new = update.status
        progressed = update.filled_quantity > rec.filled_quantity
        if new is rec.status and not progressed:
            return
        if new is not rec.status and not transition_allowed(rec.status, new):
            self.illegal_transitions += 1
            log.error("illegal order transition ignored",
                      extra={"order_id": coid, "from": rec.status.value, "to": new.value})
            self._risk_event("ILLEGAL_ORDER_TRANSITION", RiskSeverity.WARNING,
                             f"{coid}: {rec.status.value} -> {new.value} ignored", rec.strategy_id)
            if self.on_needs_reconcile:
                self.on_needs_reconcile("illegal_transition")
            return
        if progressed and update.last_fill_quantity == 0 and update.average_price:
            update = self._synthesize_fill(rec, update)
        if update.last_fill_quantity > 0 and update.last_fill_price:
            self._record_fill(rec, update)
        self.repos.orders.update_status(
            coid, new, filled_quantity=max(update.filled_quantity, rec.filled_quantity),
            average_price=update.average_price, status_message=update.message,
            broker_order_id=update.broker_order_id if self.mode is TradingMode.LIVE else None,
        )
        after = self.get(coid)
        assert after is not None
        if new is OrderStatus.REJECTED:
            self._on_rejection(coid, update.message or "rejected")
        elif new in (OrderStatus.OPEN, OrderStatus.COMPLETE, OrderStatus.PARTIALLY_FILLED):
            self.rejection_streak = 0
        if new in TERMINAL and coid in self._events:
            self._events[coid].set()
        for fn in list(self._listeners):
            try:
                fn(after, update)
            except Exception:
                log.exception("order listener failed", extra={"order_id": coid})

    def _synthesize_fill(self, rec: OrderRecord, u: OrderUpdate) -> OrderUpdate:
        """An order-book snapshot (reconciliation) shows more filled quantity
        than we saw fills for: derive the missing fill from the deltas.

        Charges are computed with the configured cost model — the same one the
        paper broker uses — with brokerage included only if this is the
        order's first fill (brokerage is charged once per order). The fill id
        is prefixed ``RECON-`` so reconstructed fills stay identifiable."""
        assert u.average_price is not None
        delta = u.filled_quantity - rec.filled_quantity
        prev_value = (rec.average_price or 0.0) * rec.filled_quantity
        price = round((u.average_price * u.filled_quantity - prev_value) / delta, 4)
        charges = self._charges.compute(rec.side, delta, price, include_brokerage=rec.filled_quantity == 0).total
        log.warning("synthesized missing fill from order book",
                    extra={"order_id": rec.client_order_id, "qty": delta, "price": price, "charges": charges})
        return u.model_copy(update={"last_fill_quantity": delta, "last_fill_price": price,
                                    "last_fill_charges": charges,
                                    "last_fill_id": f"RECON-{rec.client_order_id}-{u.filled_quantity}"})

    def _record_fill(self, rec: OrderRecord, u: OrderUpdate) -> None:
        assert rec.id is not None and u.last_fill_price is not None
        fill = FillRecord(
            order_id=rec.id, mode=self.mode, strategy_id=rec.strategy_id,
            paper_fill_id=u.last_fill_id if self.mode is TradingMode.PAPER else None,
            broker_fill_id=u.last_fill_id if self.mode is TradingMode.LIVE else None,
            quantity=u.last_fill_quantity, price=u.last_fill_price, charges=u.last_fill_charges,
            slippage=u.last_fill_slippage, filled_at=u.timestamp,
        )
        try:
            self.repos.fills.insert(fill)
        except sqlite3.IntegrityError:
            log.warning("duplicate fill ignored", extra={"order_id": rec.client_order_id, "fill_id": u.last_fill_id})

    def _on_rejection(self, coid: str, reason: str) -> None:
        rec = self.get(coid)
        sid = rec.strategy_id if rec else None
        self.rejection_streak += 1
        self._risk_event("ORDER_REJECTED", RiskSeverity.WARNING, f"{coid} rejected: {reason}", sid)
        self.alerts.send(Alert(RiskSeverity.WARNING, "Order rejected", f"{coid}: {reason}", strategy_id=sid))
        if self.rejection_streak >= self.max_rejections:
            self.kill_switch.trigger(KillReason.REPEATED_REJECTIONS,
                                     f"{self.rejection_streak} consecutive order rejections (last: {reason})")

    def _risk_event(self, event_type: str, sev: RiskSeverity, msg: str, sid: str | None) -> None:
        try:
            self.repos.risk_events.insert(RiskEventRecord(mode=self.mode, strategy_id=sid, event_type=event_type,
                                                          severity=sev, message=msg))
        except Exception:
            log.exception("failed to persist risk event")

    # ------------------------------------------------------------------ #
    # Cancel / modify / wait
    # ------------------------------------------------------------------ #
    async def cancel(self, client_order_id: str) -> OrderRecord | None:
        rec = self.get(client_order_id)
        if rec is None or rec.status in TERMINAL:
            return rec
        try:
            ack = await self.broker.cancel_order(client_order_id)
        except BrokerError as exc:
            log.error("cancel failed", extra={"order_id": client_order_id, "error": str(exc)})
            return self.get(client_order_id)
        cur = self.get(client_order_id)
        if cur is not None and ack.status is not cur.status and transition_allowed(cur.status, ack.status):
            self._set_status(client_order_id, ack.status, ack.message)
        return self.get(client_order_id)

    async def cancel_all(self, strategy_id: str | None = None, purposes: frozenset[OrderPurpose] | None = None) -> int:
        n = 0
        for rec in self.repos.orders.list_open():
            if strategy_id is not None and rec.strategy_id != strategy_id:
                continue
            if purposes is not None and rec.purpose not in purposes:
                continue
            await self.cancel(rec.client_order_id)
            n += 1
        return n

    async def modify_trigger(self, client_order_id: str, trigger_price: float) -> bool:
        rec = self.get(client_order_id)
        if rec is None or rec.status in TERMINAL:
            return False
        try:
            ack = await self.broker.modify_order(client_order_id, trigger_price=trigger_price)
        except BrokerError as exc:
            log.error("modify failed", extra={"order_id": client_order_id, "error": str(exc)})
            return False
        ok = ack.message is None or "rejected" not in ack.message
        if ok:
            self.repos.db.execute("UPDATE orders SET trigger_price = ?, updated_at = ? WHERE client_order_id = ?",
                                  (trigger_price, self._now().isoformat(), client_order_id))
        return ok

    async def wait_terminal(self, client_order_id: str, timeout: float) -> OrderRecord | None:
        """Wait until the order is terminal or ``timeout`` elapses on the
        injected clock (``sleep``): real seconds live, simulated seconds in a
        backtest, so a backtest never depends on wall-clock timers."""
        ev = self._events.get(client_order_id)
        rec = self.get(client_order_id)
        if rec is None or rec.status in TERMINAL or ev is None:
            return rec
        done = asyncio.ensure_future(ev.wait())
        timer = asyncio.ensure_future(self._sleep(timeout))
        try:
            await asyncio.wait({done, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (done, timer):
                fut.cancel()
            await asyncio.gather(done, timer, return_exceptions=True)
        return self.get(client_order_id)

    async def resolve_unknown(self, client_order_id: str) -> OrderStatus:
        """Ask the broker for the real state of an UNKNOWN order."""
        try:
            orders = await self.broker.get_orders()
        except BrokerError:
            return OrderStatus.UNKNOWN
        for u in orders:
            if u.client_order_id == client_order_id:
                self.on_broker_update(u)
                break
        rec = self.get(client_order_id)
        return rec.status if rec else OrderStatus.UNKNOWN

    async def expire_stale_entries(self) -> int:
        """Cancel ENTRY orders still working after ``entry_timeout_seconds``."""
        n = 0
        now = self._now()
        for rec in self.repos.orders.list_open():
            if rec.purpose is OrderPurpose.ENTRY and (now - rec.created_at).total_seconds() >= \
                    self.settings.entry_timeout_seconds:
                await self.cancel(rec.client_order_id)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    # Exits with safe retries
    # ------------------------------------------------------------------ #
    async def exit_position(
        self,
        *,
        strategy_id: str,
        trade_uid: str | None,
        instrument: Instrument,
        remaining_qty: Callable[[], int],
        purpose: OrderPurpose,
        wait_seconds: float = 2.0,
    ) -> bool:
        if purpose not in EXIT_PURPOSES:
            raise ValueError("exit_position requires an exit purpose")
        s = self.settings
        for attempt in range(s.exit_max_attempts):
            qty = remaining_qty()
            if qty <= 0:
                return True
            rec = await self.submit(OrderIntent(strategy_id, instrument, OrderSide.SELL, qty, OrderType.MARKET,
                                                purpose, trade_uid))
            if rec.status not in TERMINAL:
                rec = await self.wait_terminal(rec.client_order_id, wait_seconds) or rec
            if rec.status is OrderStatus.UNKNOWN:
                # Never re-send until we know whether the last attempt executed.
                for _ in range(3):
                    if await self.resolve_unknown(rec.client_order_id) is not OrderStatus.UNKNOWN:
                        break
                    await self._sleep(s.exit_retry_base_seconds)
                rec = self.get(rec.client_order_id) or rec
                if rec.status is OrderStatus.UNKNOWN:
                    break  # cannot safely retry -> escalate
            if rec.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMITTED):
                await self.cancel(rec.client_order_id)  # replace the remainder on the next attempt
            if remaining_qty() <= 0:
                return True
            delay = backoff_delay(attempt, s.exit_retry_base_seconds, s.exit_retry_max_seconds)
            log.warning("exit attempt incomplete; retrying",
                        extra={"strategy_id": strategy_id, "attempt": attempt + 1, "remaining": remaining_qty(),
                               "status": rec.status.value, "delay_s": round(delay, 2)})
            await self._sleep(delay)
        if remaining_qty() <= 0:
            return True
        msg = (f"could not exit {remaining_qty()} x {instrument.trading_symbol} for {strategy_id} after "
               f"{s.exit_max_attempts} attempts - MANUAL INTERVENTION REQUIRED")
        self.alerts.send(Alert(RiskSeverity.CRITICAL, "EXIT FAILED", msg, strategy_id=strategy_id))
        self._risk_event("EXIT_FAILED", RiskSeverity.CRITICAL, msg, strategy_id)
        self.kill_switch.trigger(KillReason.EXIT_FAILURE, msg)
        return False
