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
from core.models import Instrument, OrderUpdate, Tick
from data.storage.records import OrderRecord, PositionRecord, TradeRecord
from data.storage.repositories import Repositories
from execution.order_manager import TERMINAL, EntryBlockedError, OrderIntent, OrderManager
from monitoring.json_logger import get_logger
from risk.daily_loss_guard import DailyLossGuard
from risk.risk_manager import OpenTradeCounter, RiskDecision
from risk.stop_loss_manager import ExitReason, ProtectiveLevels, check_exit, r_multiple
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
}


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
        self.repos.trades.update(t.trade_uid, entry_price=round(t.avg_entry, 4), entry_time=self._now(),
                                 charges=round(t.charges, 2))
        self._sync_position(t)

    def _apply_exit_fill(self, t: ManagedTrade, qty: int, price: float, charges: float, purpose: OrderPurpose) -> None:
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

    async def _place_protective_stop(self, t: ManagedTrade) -> None:
        if t.open_qty <= 0 or t.protective_order_id is not None or t.exit_in_progress:
            return
        try:
            rec = await self.orders.submit(OrderIntent(t.strategy_id, t.instrument, OrderSide.SELL, t.open_qty,
                                                       OrderType.SL_M, OrderPurpose.STOP_LOSS, t.trade_uid,
                                                       trigger_price=t.stop))
        except Exception:
            log.exception("failed to place protective stop; local monitoring remains active",
                          extra={"strategy_id": t.strategy_id})
            return
        self._by_order[rec.client_order_id] = t.trade_uid
        if rec.status not in TERMINAL:
            t.protective_order_id = rec.client_order_id

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
            self._spawn(t, self._cancel_quiet(t.protective_order_id))

    async def _cancel_quiet(self, coid: str) -> None:
        await self.orders.cancel(coid)

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
        if t.exit_in_progress or t.status is not TradeStatus.OPEN:
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
            await self.wait_for(t)
            return t.open_qty == 0
        t.exit_in_progress = True
        return await self._exit(t, reason)

    async def _exit(self, t: ManagedTrade, reason: ExitReason) -> bool:
        t.exit_reason = t.exit_reason or reason
        try:
            if t.entry_order_id and not t.entry_complete:
                await self.orders.cancel(t.entry_order_id)  # stop adding to a position we are leaving
            if t.protective_order_id is not None:
                await self.orders.cancel(t.protective_order_id)
                t.protective_order_id = None
            if t.open_qty <= 0:
                return True
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
                             entry_complete=True)
            for o in orders:
                self._by_order[o.client_order_id] = rec.trade_uid
                if o.purpose is OrderPurpose.STOP_LOSS and o.status not in TERMINAL:
                    t.protective_order_id = o.client_order_id
            self.trades[rec.trade_uid] = t
        if self.trades:
            log.warning("restored open trades after restart", extra={"count": len(self.open_trades()),
                                                                     "unrestorable": missing})
        return missing

