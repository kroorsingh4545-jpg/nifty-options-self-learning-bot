"""Per-strategy trades and positions, built from fills; real-time protection.

For every trade:
* ENTRY fills build the position (average price, entry time). When the entry
  order finishes, an exchange-side protective SL-M SELL order is placed at the
  stop for the filled quantity (``use_exchange_stop_orders``).
* Every tick: mark-to-market, trailing-stop update (tighten-only; the resting
  SL-M trigger is modified to follow), and local stop / target / structural
  checks. A local trigger cancels the resting stop, then exits at market.
* EXIT fills reduce the position; when flat the trade closes with gross P&L,
  charges, net P&L and R-multiple, and the daily loss guard is updated.

Trade-manager advice (Phase 8, ``apply_advice``): the RL agent may ask to
tighten the stop, take a partial profit or exit an OPEN trade of its own
strategy. Every request is re-validated here against the live trade: stops
only tighten (``tighten_stop``) and stay strictly below the market, a partial
exit keeps at least one lot and only happens in profit, nothing is accepted
for a trade that is exiting, or at/after the square-off start (the square-off
manager owns the end of the day). Limits, exposure and the kill switch are
not reachable from advice at all.

Resting-SELL invariant: the quantity of LIVE sell orders for a trade (the
protective SL-M plus any exit order) never exceeds the quantity held, so no
stop or retry can ever sell more than the position (a short option).

* Placing the protective stop and cancelling it are serialised per trade
  (``ManagedTrade.protect_lock``); an exit waits for an in-flight placement
  and then cancels it.
* A protective stop is sized to what is not already on a live SELL order
  (``sellable_qty``).
* A cancel counts only when the broker confirms the order is terminal
  (``OrderManager.cancel_confirmed``). If the resting stop cannot be
  cancelled, an exit is NOT sent (the stop keeps protecting the position) and
  the failure escalates (CRITICAL alert + kill switch); a partial exit is
  simply abandoned.
* An exit is never sent while another SELL order of the trade is still live,
  and exit retries never re-send while the previous attempt may be live.

Strategy isolation: trades and positions are keyed by strategy; a strategy
can never exit or modify another strategy's trade.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config.settings import BotConfig, TrailingConfig
from core.clock import now_ist
from core.enums import (
    OptionType,
    OrderPurpose,
    OrderSide,
    OrderType,
    TradeStatus,
    TradingMode,
)
from core.ids import new_trade_uid
from core.interfaces import STOP_ADVICE, TradeAdvice, TradeAdviceAction
from core.models import Instrument, OrderUpdate, Tick
from data.storage.records import OrderRecord, PositionRecord, TradeRecord
from data.storage.repositories import Repositories
from execution.order_manager import TERMINAL, EntryBlockedError, OrderIntent, OrderManager
from monitoring.json_logger import get_logger
from risk.daily_loss_guard import DailyLossGuard
from risk.risk_manager import OpenTradeCounter, RiskDecision
from risk.stop_loss_manager import (
    ExitReason,
    ProtectiveLevels,
    StopWideningError,
    _ceil_tick,
    check_exit,
    r_multiple,
    tighten_stop,
)
from risk.trailing_stop_manager import TrailingState, TrailingStopManager

log = get_logger(__name__)

EXIT_PURPOSE = {
    ExitReason.STOP_LOSS: OrderPurpose.STOP_LOSS,
    ExitReason.TRAILING_STOP: OrderPurpose.STOP_LOSS,
    ExitReason.STRUCTURAL_STOP: OrderPurpose.STOP_LOSS,
    ExitReason.TARGET: OrderPurpose.TARGET,
    ExitReason.SQUARE_OFF: OrderPurpose.SQUARE_OFF,
    ExitReason.KILL_SWITCH: OrderPurpose.KILL_SWITCH,
    ExitReason.MANUAL: OrderPurpose.EXIT,
    ExitReason.STRATEGY: OrderPurpose.EXIT,
    ExitReason.TRADE_MANAGER: OrderPurpose.EXIT,
}


@dataclass(frozen=True)
class AdviceResult:
    applied: bool
    reason: str


@dataclass
class ManagedTrade:
    trade_uid: str
    strategy_id: str
    instrument: Instrument
    option_type: OptionType
    levels: ProtectiveLevels
    trailing: TrailingStopManager
    trail: TrailingState
    requested_qty: int
    filled_qty: int = 0
    exited_qty: int = 0
    avg_entry: float = 0.0
    exit_value: float = 0.0
    charges: float = 0.0
    entry_complete: bool = False
    status: TradeStatus = TradeStatus.OPEN
    entry_order_id: str | None = None
    protective_order_id: str | None = None
    exit_in_progress: bool = False
    exit_reason: ExitReason | None = None
    last_price: float | None = None
    entry_time: datetime | None = None
    partial_in_progress: bool = False
    partial_exits: int = 0
    escalate_exit: ExitReason | None = None  # stop/target hit while a partial exit was running
    # serialises protective-stop placement and cancellation (resting-SELL invariant)
    protect_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def open_qty(self) -> int:
        return self.filled_qty - self.exited_qty

    @property
    def stop(self) -> float:
        return self.trail.stop

    @property
    def unrealized(self) -> float:
        if self.last_price is None or self.open_qty <= 0:
            return 0.0
        return (self.last_price - self.avg_entry) * self.open_qty


class PositionManager(OpenTradeCounter):
    def __init__(
        self,
        cfg: BotConfig,
        *,
        repos: Repositories,
        orders: OrderManager,
        guard: DailyLossGuard,
        now_fn: Callable[[], datetime] = now_ist,
        on_tick_checked: Callable[[Tick, bool], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.repos = repos
        self.mode: TradingMode = repos.mode
        self.orders = orders
        self.guard = guard
        self._now = now_fn
        self.on_tick_checked = on_tick_checked  # e.g. RiskManager.check_spread
        self.trades: dict[str, ManagedTrade] = {}
        self._by_order: dict[str, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        orders.add_listener(self.on_order_event)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def open_trades(self, strategy_id: str | None = None) -> list[ManagedTrade]:
        return [t for t in self.trades.values() if t.status is TradeStatus.OPEN
                and (strategy_id is None or t.strategy_id == strategy_id)]

    def open_trade_count(self, strategy_id: str | None = None) -> int:
        return len(self.open_trades(strategy_id))

    def holds(self, instrument_key: str) -> bool:
        return any(t.instrument.instrument_key == instrument_key and t.open_qty > 0 for t in self.open_trades())

    def net_quantities(self) -> dict[str, int]:
        """Net open quantity per instrument across all strategies (for reconciliation)."""
        out: dict[str, int] = {}
        for t in self.open_trades():
            if t.open_qty:
                key = t.instrument.instrument_key
                out[key] = out.get(key, 0) + t.open_qty
        return out

    def unrealized_by_strategy(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.open_trades():
            out[t.strategy_id] = out.get(t.strategy_id, 0.0) + t.unrealized
        return out

    # ------------------------------------------------------------------ #
    # Opening a trade
    # ------------------------------------------------------------------ #
    async def open_trade(self, decision: RiskDecision, *, strategy_version: str) -> ManagedTrade:
        if not decision.approved or decision.levels is None or decision.sizing is None or decision.entry_price is None:
            raise ValueError("only an approved RiskDecision can open a trade")
        p = decision.proposal
        inst = p.instrument
        trail_cfg: TrailingConfig = decision.trailing or self.cfg.strategies.strategies[p.strategy_id].trailing
        mgr = TrailingStopManager(trail_cfg, p.option_type, inst.tick_size)
        uid = new_trade_uid(self.mode, p.strategy_id)
        lv = decision.levels
        assert inst.strike is not None and inst.expiry is not None
        self.repos.trades.insert(TradeRecord(
            trade_uid=uid, mode=self.mode, strategy_id=p.strategy_id, strategy_version=strategy_version,
            instrument_key=inst.instrument_key, trading_symbol=inst.trading_symbol, option_type=p.option_type,
            strike=inst.strike, expiry=inst.expiry, side=OrderSide.BUY, quantity=decision.sizing.quantity,
            initial_stop_loss=lv.stop, stop_loss=lv.stop, target=lv.target,
            trailing_config_json=trail_cfg.model_dump(mode="json"), entry_reason=p.reason,
            ml_confidence=p.ml_confidence, regime=p.regime, created_at=self._now(), updated_at=self._now(),
        ))
        trade = ManagedTrade(uid, p.strategy_id, inst, p.option_type, lv, mgr, mgr.new_state(lv.entry, lv.stop),
                             decision.sizing.quantity)
        self.trades[uid] = trade
        self.guard.record_entry(p.strategy_id)
        buffer = self.cfg.settings.execution.entry_limit_buffer_ticks * inst.tick_size
        limit = round(math.ceil((decision.entry_price + buffer) / inst.tick_size - 1e-9) * inst.tick_size, 2)
        try:
            rec = await self.orders.submit(OrderIntent(p.strategy_id, inst, OrderSide.BUY, trade.requested_qty,
                                                       OrderType.LIMIT, OrderPurpose.ENTRY, uid, price=limit))
        except EntryBlockedError:
            self._cancel_trade(trade, "entry blocked by kill switch")
            raise
        trade.entry_order_id = rec.client_order_id
        self._by_order[rec.client_order_id] = uid
        if rec.status in TERMINAL:  # paper fills can complete synchronously
            self._on_entry_terminal(trade, rec)
        return trade

    def _cancel_trade(self, trade: ManagedTrade, reason: str) -> None:
        trade.status = TradeStatus.CANCELLED
        self.repos.trades.update(trade.trade_uid, status=TradeStatus.CANCELLED, exit_reason=reason)
        log.info("trade cancelled before fill", extra={"strategy_id": trade.strategy_id, "trade": trade.trade_uid,
                                                       "reason": reason})

    # ------------------------------------------------------------------ #
    # Order events
    # ------------------------------------------------------------------ #
    def on_order_event(self, rec: OrderRecord, u: OrderUpdate) -> None:
        uid = rec.trade_uid or self._by_order.get(rec.client_order_id)
        trade = self.trades.get(uid) if uid else None
        if trade is None:
            return
        if trade.strategy_id != rec.strategy_id:  # defence in depth (schema enforces too)
            log.critical("order/trade strategy mismatch ignored", extra={"order_id": rec.client_order_id})
            return
        self._by_order.setdefault(rec.client_order_id, trade.trade_uid)
        if u.last_fill_quantity > 0 and u.last_fill_price is not None:
            if rec.side is OrderSide.BUY:
                self._apply_entry_fill(trade, u.last_fill_quantity, u.last_fill_price, u.last_fill_charges)
            else:
                self._apply_exit_fill(trade, u.last_fill_quantity, u.last_fill_price, u.last_fill_charges,
                                      rec.purpose)
        if rec.purpose is OrderPurpose.ENTRY and rec.status in TERMINAL and not trade.entry_complete \
                and rec.client_order_id == trade.entry_order_id:
            self._on_entry_terminal(trade, rec)
        if rec.client_order_id == trade.protective_order_id and rec.status in TERMINAL:
            trade.protective_order_id = None

    def _apply_entry_fill(self, t: ManagedTrade, qty: int, price: float, charges: float) -> None:
        total = t.avg_entry * t.filled_qty + qty * price
        t.filled_qty += qty
        t.avg_entry = total / t.filled_qty
        t.charges += charges
        t.last_price = price
        if t.entry_time is None:
            t.entry_time = self._now()
        self.repos.trades.update(t.trade_uid, entry_price=round(t.avg_entry, 4), entry_time=self._now(),
                                 charges=round(t.charges, 2))
        self._sync_position(t)

    def _apply_exit_fill(self, t: ManagedTrade, qty: int, price: float, charges: float, purpose: OrderPurpose) -> None:
        if qty > t.open_qty:  # sold more than held: the broker position is now short
            self.orders.escalate_exit_failure(
                t.strategy_id, f"{t.trade_uid}: SELL fill of {qty} exceeds the {t.open_qty} held "
                               f"({purpose.value}) - position may be SHORT")
        qty = min(qty, t.open_qty)
        realized = (price - t.avg_entry) * qty
        t.exited_qty += qty
        t.exit_value += qty * price
        t.charges += charges
        if t.exit_reason is None:
            reason = {OrderPurpose.STOP_LOSS: ExitReason.STOP_LOSS, OrderPurpose.TARGET: ExitReason.TARGET,
                      OrderPurpose.SQUARE_OFF: ExitReason.SQUARE_OFF,
                      OrderPurpose.KILL_SWITCH: ExitReason.KILL_SWITCH}.get(purpose, ExitReason.MANUAL)
            if reason is ExitReason.STOP_LOSS and t.trail.active:
                reason = ExitReason.TRAILING_STOP  # the resting stop had been trailed (label only)
            t.exit_reason = reason
        self._sync_position(t, realized_delta=realized)
        if t.open_qty == 0 and t.entry_complete:
            self._close_trade(t)

    def _on_entry_terminal(self, t: ManagedTrade, rec: OrderRecord) -> None:
        t.entry_complete = True
        if t.filled_qty == 0:
            self._cancel_trade(t, f"entry {rec.status.value.lower()} without fill")
            return
        if t.filled_qty != t.requested_qty:
            self.repos.trades.update(t.trade_uid, quantity=t.filled_qty)
        log.info("trade opened", extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "qty": t.filled_qty,
                                        "avg": round(t.avg_entry, 2), "stop": t.stop, "target": t.levels.target})
        if t.open_qty == 0:
            self._close_trade(t)
        elif self.cfg.settings.execution.use_exchange_stop_orders and not t.exit_in_progress:
            self._spawn(t, self._place_protective_stop(t))

    # ------------------------------------------------------------------ #
    # Resting-SELL invariant
    # ------------------------------------------------------------------ #
    def resting_sell_qty(self, t: ManagedTrade) -> int:
        """Unfilled quantity on this trade's live (non-terminal) SELL orders."""
        return sum(o.quantity - o.filled_quantity for o in self.repos.orders.list_for_trade(t.trade_uid)
                   if o.side is OrderSide.SELL and o.status not in TERMINAL)

    def sellable_qty(self, t: ManagedTrade) -> int:
        """Held quantity not already reserved by a live SELL order."""
        return max(0, t.open_qty - self.resting_sell_qty(t))

    async def _place_protective_stop(self, t: ManagedTrade) -> None:
        async with t.protect_lock:
            if t.open_qty <= 0 or t.protective_order_id is not None or t.exit_in_progress \
                    or t.status is not TradeStatus.OPEN:
                return
            qty = self.sellable_qty(t)
            if qty <= 0:
                log.warning("protective stop not placed: live SELL orders already cover the position",
                            extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "open": t.open_qty})
                return
            if qty < t.open_qty:
                log.warning("protective stop sized to the unreserved quantity (live SELL orders exist)",
                            extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "open": t.open_qty,
                                   "qty": qty})
            try:
                rec = await self.orders.submit(OrderIntent(t.strategy_id, t.instrument, OrderSide.SELL, qty,
                                                           OrderType.SL_M, OrderPurpose.STOP_LOSS, t.trade_uid,
                                                           trigger_price=t.stop))
            except Exception:
                log.exception("failed to place protective stop; local monitoring remains active",
                              extra={"strategy_id": t.strategy_id})
                return
            self._by_order[rec.client_order_id] = t.trade_uid
            if rec.status not in TERMINAL:
                t.protective_order_id = rec.client_order_id

    async def _release_protective(self, t: ManagedTrade) -> bool:
        """Cancel the resting stop (after any in-flight placement) and confirm
        it is gone. ``False``: it may still be live and keeps being tracked."""
        async with t.protect_lock:
            coid = t.protective_order_id
            if coid is None:
                return True
            if not await self.orders.cancel_confirmed(coid):
                return False
            if t.protective_order_id == coid:
                t.protective_order_id = None
            return True

    def _close_trade(self, t: ManagedTrade) -> None:
        if t.status is not TradeStatus.OPEN:
            return
        avg_exit = t.exit_value / t.exited_qty if t.exited_qty else t.avg_entry
        gross = (avg_exit - t.avg_entry) * t.exited_qty
        t.status = TradeStatus.CLOSED
        reason = (t.exit_reason or ExitReason.MANUAL).value
        self.repos.trades.close(t.trade_uid, exit_price=round(avg_exit, 4), exit_reason=reason,
                                gross_pnl=round(gross, 2), charges=round(t.charges, 2),
                                r_multiple=r_multiple(t.avg_entry, avg_exit, t.levels.risk_per_unit))
        net = gross - t.charges
        log.info("trade closed", extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "reason": reason,
                                        "gross": round(gross, 2), "net": round(net, 2)})
        self.guard.record_trade_close(t.strategy_id, round(net, 2), round(t.charges, 2))
        self.guard.update_unrealized(t.strategy_id, self.unrealized_by_strategy().get(t.strategy_id, 0.0))
        if t.protective_order_id is not None:
            self._spawn(t, self._cancel_after_close(t))

    async def _cancel_after_close(self, t: ManagedTrade) -> None:
        if not await self._release_protective(t):
            self.orders.escalate_exit_failure(t.strategy_id, f"resting stop of closed trade {t.trade_uid} could not "
                                                             "be cancelled and could sell a position that is flat")

    def _sync_position(self, t: ManagedTrade, realized_delta: float = 0.0) -> None:
        key = t.instrument.instrument_key
        group = [x for x in self.trades.values() if x.strategy_id == t.strategy_id
                 and x.instrument.instrument_key == key and x.status is TradeStatus.OPEN]
        qty = sum(x.open_qty for x in group)
        avg = (sum(x.avg_entry * x.open_qty for x in group) / qty) if qty else 0.0
        stop = min((x.stop for x in group), default=t.stop)
        existing = self.repos.positions.get_open(t.strategy_id, key)
        if existing is None:
            if qty > 0:
                self.repos.positions.insert(PositionRecord(
                    mode=self.mode, strategy_id=t.strategy_id, instrument_key=key,
                    trading_symbol=t.instrument.trading_symbol, quantity=qty, average_price=round(avg, 4),
                    last_price=t.last_price, stop_loss=stop, target=t.levels.target, trailing_stop=t.stop))
            return
        assert existing.id is not None
        realized = round(existing.realized_pnl + realized_delta, 2)
        if qty == 0:
            self.repos.positions.close(existing.id, realized_pnl=realized)
        else:
            self.repos.positions.update(existing.id, quantity=qty, average_price=round(avg, 4), realized_pnl=realized,
                                        stop_loss=stop, trailing_stop=t.stop, last_price=t.last_price)

    # ------------------------------------------------------------------ #
    # Ticks: MTM, trailing, triggers
    # ------------------------------------------------------------------ #
    def on_tick(self, tick: Tick) -> None:
        held = False
        touched: set[str] = set()
        for t in self.open_trades():
            if t.instrument.instrument_key != tick.instrument_key or t.open_qty <= 0:
                continue
            held = True
            touched.add(t.strategy_id)
            t.last_price = tick.ltp
            if t.exit_in_progress:
                if t.partial_in_progress:  # protection never pauses during a partial exit
                    hit = check_exit(tick.ltp, t.stop, t.levels.target)
                    if hit is not None:
                        self.request_exit(t, hit)
                continue
            if t.trailing.update(t.trail, tick.ltp):
                self.repos.trades.update(t.trade_uid, stop_loss=t.stop)
                self._sync_position(t)
                if t.protective_order_id is not None:
                    self._spawn(t, self._move_protective(t))
            hit = check_exit(tick.ltp, t.stop, t.levels.target)
            if hit is ExitReason.STOP_LOSS and t.trail.active:
                hit = ExitReason.TRAILING_STOP
            if hit is not None:
                self.request_exit(t, hit)
        for sid in touched:
            self.guard.update_unrealized(sid, self.unrealized_by_strategy().get(sid, 0.0))
        if self.on_tick_checked is not None:
            self.on_tick_checked(tick, held)

    def on_underlying(self, price: float, strategy_id: str | None = None) -> None:
        """Underlying (NIFTY) price for STRUCTURAL trailing stops."""
        for t in self.open_trades(strategy_id):
            if t.open_qty > 0 and not t.exit_in_progress and t.trailing.structural_breached(t.trail, price):
                self.request_exit(t, ExitReason.STRUCTURAL_STOP)

    def set_structure_level(self, trade_uid: str, level: float) -> bool:
        t = self.trades[trade_uid]
        return t.trailing.update_structure(t.trail, level)

    async def _move_protective(self, t: ManagedTrade) -> None:
        if t.protective_order_id is not None:
            await self.orders.modify_trigger(t.protective_order_id, t.stop)

    # ------------------------------------------------------------------ #
    # Exits
    # ------------------------------------------------------------------ #
    def request_exit(self, t: ManagedTrade, reason: ExitReason) -> None:
        if t.status is not TradeStatus.OPEN:
            return
        if t.exit_in_progress:
            if t.partial_in_progress and t.escalate_exit is None:
                t.escalate_exit = reason  # exit the rest as soon as the partial exit finishes
            return
        t.exit_in_progress = True
        self._spawn(t, self._exit(t, reason))

    async def exit_trade(self, trade_uid: str, reason: ExitReason, *, strategy_id: str | None = None) -> bool:
        t = self.trades.get(trade_uid)
        if t is None or t.status is not TradeStatus.OPEN:
            return True
        if strategy_id is not None and t.strategy_id != strategy_id:
            raise PermissionError("a strategy may only exit its own trades")
        if t.exit_in_progress:
            partial = t.partial_in_progress
            await self.wait_for(t)
            if partial and t.status is TradeStatus.OPEN and t.open_qty > 0 and not t.exit_in_progress:
                t.exit_in_progress = True  # a partial exit finished; now exit the rest
                return await self._exit(t, reason)
            return t.open_qty == 0
        t.exit_in_progress = True
        return await self._exit(t, reason)

    async def _exit(self, t: ManagedTrade, reason: ExitReason) -> bool:
        t.exit_reason = t.exit_reason or reason
        try:
            if t.entry_order_id and not t.entry_complete:
                await self.orders.cancel(t.entry_order_id)  # stop adding to a position we are leaving
            if not await self._release_protective(t):
                # Selling now could oversell if the stop also triggers; the stop keeps protecting the position.
                self.orders.escalate_exit_failure(
                    t.strategy_id, f"resting stop of {t.trade_uid} could not be cancelled; exit NOT sent "
                                   "(it could sell more than is held)")
                return False
            if t.open_qty <= 0:
                return True
            live = self.resting_sell_qty(t)
            if live > 0:
                self.orders.escalate_exit_failure(
                    t.strategy_id, f"{live} of {t.trade_uid} is already on live SELL orders; no further exit sent")
                return False
            log.info("exiting trade", extra={"strategy_id": t.strategy_id, "trade": t.trade_uid,
                                             "reason": reason.value, "qty": t.open_qty})
            ok = await self.orders.exit_position(strategy_id=t.strategy_id, trade_uid=t.trade_uid,
                                                 instrument=t.instrument, remaining_qty=lambda: t.open_qty,
                                                 purpose=EXIT_PURPOSE[reason])
            return ok
        finally:
            t.exit_in_progress = False
            protect = self.cfg.settings.execution.use_exchange_stop_orders
            if t.open_qty > 0 and t.status is TradeStatus.OPEN and protect and t.protective_order_id is None \
                    and t.entry_complete:
                # exit failed: put protection back while humans intervene
                self._spawn(t, self._place_protective_stop(t))

    # ------------------------------------------------------------------ #
    # Trade-manager (RL) advice
    # ------------------------------------------------------------------ #
    def apply_advice(self, advice: TradeAdvice) -> AdviceResult:
        """Apply a trade manager's proposal after re-validating it (see the
        module docstring). Never raises for a refused proposal."""
        t = self.trades.get(advice.trade_uid)
        if t is None or t.status is not TradeStatus.OPEN:
            return self._refuse(advice, "no open trade with this id")
        if t.strategy_id != advice.strategy_id:
            return self._refuse(advice, "a strategy may only manage its own trades")
        scfg = self.cfg.strategies.strategies.get(t.strategy_id)
        if scfg is None or not scfg.rl.enabled:
            return self._refuse(advice, "the RL trade manager is not enabled for this strategy")
        if not t.entry_complete or t.open_qty <= 0 or t.exit_in_progress:
            return self._refuse(advice, "trade is not idle (entry incomplete, flat or exiting)")
        if self._now().time() >= self.cfg.risk.time_rules.square_off_start:
            return self._refuse(advice, "square-off window: the square-off manager owns exits")
        a = TradeAdviceAction(advice.action)
        if a is TradeAdviceAction.HOLD:
            return AdviceResult(True, "hold")
        tick = t.instrument.tick_size
        ltp = t.last_price
        if ltp is None or ltp <= 0:
            return self._refuse(advice, "no market price for the trade")
        if a in STOP_ADVICE:
            assert advice.new_stop is not None
            new = _ceil_tick(advice.new_stop, tick)
            if new > ltp - tick + 1e-9:
                return self._refuse(advice, f"stop {new} is not strictly below the market {ltp}")
            try:
                tightened = tighten_stop(t.stop, new)
            except StopWideningError as exc:
                return self._refuse(advice, str(exc))
            if tightened <= t.stop + 1e-9:
                return self._refuse(advice, "stop would not tighten")
            old = t.stop
            t.trail.stop = round(tightened, 2)
            self.repos.trades.update(t.trade_uid, stop_loss=t.stop)
            self._sync_position(t)
            if t.protective_order_id is not None:
                self._spawn(t, self._move_protective(t))
            log.info("trade manager tightened stop", extra={"strategy_id": t.strategy_id, "trade": t.trade_uid,
                                                            "action": a.value, "old": old, "new": t.stop,
                                                            "model": advice.model_version})
            return AdviceResult(True, f"stop {old} -> {t.stop}")
        if a is TradeAdviceAction.EXIT_FULL:
            log.info("trade manager requested a full exit", extra={"strategy_id": t.strategy_id,
                                                                   "trade": t.trade_uid, "model": advice.model_version})
            self.request_exit(t, ExitReason.TRADE_MANAGER)
            return AdviceResult(True, "full exit requested")
        # TAKE_PARTIAL_PROFIT
        lot = t.instrument.lot_size
        qty = advice.exit_lots * lot
        if qty >= t.open_qty:
            return self._refuse(advice, "a partial exit must keep at least one lot")
        if ltp <= t.avg_entry:
            return self._refuse(advice, "a partial profit needs the trade to be in profit")
        if t.partial_exits >= scfg.rl.max_partial_exits:
            return self._refuse(advice, f"at most {scfg.rl.max_partial_exits} partial exit(s) per trade")
        if t.protect_lock.locked():  # the lock alone keeps the invariant; this only avoids churn
            return self._refuse(advice, "trade is busy (protective stop being placed or cancelled)")
        t.exit_in_progress = True
        t.partial_in_progress = True
        keep = t.open_qty - qty  # fixed NOW: the quantity that must still be held after the partial
        self._spawn(t, self._partial_exit(t, keep))
        log.info("trade manager partial exit", extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "qty": qty,
                                                      "keep": keep, "model": advice.model_version})
        return AdviceResult(True, f"partial exit of {qty}")

    def _refuse(self, advice: TradeAdvice, reason: str) -> AdviceResult:
        log.warning("trade manager advice refused", extra={"strategy_id": advice.strategy_id,
                                                           "trade": advice.trade_uid, "action": str(advice.action),
                                                           "reason": reason})
        return AdviceResult(False, reason)

    async def _partial_exit(self, t: ManagedTrade, keep: int) -> None:
        """Sell down to ``keep`` (fixed when the partial was requested, >= 1
        lot). Never sells below ``keep`` and never more than is held."""
        try:
            if keep < t.instrument.lot_size or t.status is not TradeStatus.OPEN or t.open_qty <= keep:
                log.warning("partial exit abandoned: the position is already at or below the quantity to keep",
                            extra={"strategy_id": t.strategy_id, "trade": t.trade_uid, "open": t.open_qty,
                                   "keep": keep})
                return
            if not await self._release_protective(t):
                log.critical("partial exit abandoned: the resting stop could not be cancelled and stays in force",
                             extra={"strategy_id": t.strategy_id, "trade": t.trade_uid})
                return
            if self.resting_sell_qty(t) > 0:
                log.warning("partial exit abandoned: other SELL orders of the trade are live",
                            extra={"strategy_id": t.strategy_id, "trade": t.trade_uid})
                return
            ok = await self.orders.exit_position(strategy_id=t.strategy_id, trade_uid=t.trade_uid,
                                                 instrument=t.instrument,
                                                 remaining_qty=lambda: max(0, min(t.open_qty, t.open_qty - keep)),
                                                 purpose=OrderPurpose.EXIT)
            if ok:
                t.partial_exits += 1
        finally:
            t.partial_in_progress = False
            t.exit_in_progress = False
            if t.status is TradeStatus.OPEN and t.open_qty > 0:
                t.exit_reason = None  # partial fills do not label the trade's final exit
            escalate, t.escalate_exit = t.escalate_exit, None
            protect = self.cfg.settings.execution.use_exchange_stop_orders
            if escalate is not None and t.status is TradeStatus.OPEN and t.open_qty > 0:
                t.exit_in_progress = True
                await self._exit(t, escalate)  # re-protects itself if the exit fails
            elif t.status is TradeStatus.OPEN and t.open_qty > 0 and t.protective_order_id is None and protect:
                await self._place_protective_stop(t)

    async def exit_all(self, reason: ExitReason, strategy_id: str | None = None) -> bool:
        trades = self.open_trades(strategy_id)
        results = await asyncio.gather(*(self.exit_trade(t.trade_uid, reason) for t in trades),
                                       return_exceptions=True)
        return all(r is True for r in results)

    def _spawn(self, t: ManagedTrade, coro: Coroutine[Any, Any, Any]) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        t.tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(t.tasks.discard)

    async def wait_for(self, t: ManagedTrade) -> None:
        while t.tasks:
            await asyncio.gather(*list(t.tasks), return_exceptions=True)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Restart recovery
    # ------------------------------------------------------------------ #
    def restore(self, instruments: dict[str, Instrument]) -> list[str]:
        """Rebuild OPEN trades from the database after a restart. Returns
        trade uids that could not be restored (unknown instrument)."""
        missing: list[str] = []
        for rec in self.repos.trades.list_open():
            inst = instruments.get(rec.instrument_key)
            if inst is None or rec.entry_price is None:
                missing.append(rec.trade_uid)
                continue
            scfg = self.cfg.strategies.strategies.get(rec.strategy_id)
            trail_cfg = TrailingConfig.model_validate(rec.trailing_config_json) if rec.trailing_config_json \
                else scfg.trailing if scfg else None
            if trail_cfg is None:
                missing.append(rec.trade_uid)
                continue
            levels = ProtectiveLevels(rec.entry_price, rec.initial_stop_loss, rec.target,
                                      rec.entry_price - rec.initial_stop_loss)
            mgr = TrailingStopManager(trail_cfg, rec.option_type, inst.tick_size)
            state = mgr.new_state(rec.entry_price, rec.initial_stop_loss)
            state.stop = rec.stop_loss
            state.active = rec.stop_loss > rec.initial_stop_loss
            orders = self.repos.orders.list_for_trade(rec.trade_uid)
            bought = sum(o.filled_quantity for o in orders if o.side is OrderSide.BUY)
            sold = sum(o.filled_quantity for o in orders if o.side is OrderSide.SELL)
            t = ManagedTrade(rec.trade_uid, rec.strategy_id, inst, rec.option_type, levels, mgr, state, rec.quantity,
                             filled_qty=bought, exited_qty=sold, avg_entry=rec.entry_price, charges=rec.charges,
                             entry_complete=True, entry_time=rec.entry_time)
            for o in orders:
                self._by_order[o.client_order_id] = rec.trade_uid
                if o.purpose is OrderPurpose.STOP_LOSS and o.status not in TERMINAL:
                    t.protective_order_id = o.client_order_id
            self.trades[rec.trade_uid] = t
        if self.trades:
            log.warning("restored open trades after restart", extra={"count": len(self.open_trades()),
                                                                     "unrestorable": missing})
        return missing

