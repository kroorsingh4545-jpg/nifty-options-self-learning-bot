"""Wire risk + execution together (used by the Phase 4 engine and tests).

Event flow::

    tick ─▶ MarketDataService ─▶ PaperBroker.on_tick (fills resting orders)
                              └▶ PositionManager.on_tick ─▶ trailing / SL / TP
                                                          └▶ RiskManager.check_spread
    broker order update ─▶ OrderManager ─▶ PositionManager (fills -> trades)
                                       └▶ Reconciler.request (debounced)
    DailyLossGuard breach ─▶ KillSwitch.trigger ─▶ SquareOffManager.on_kill
                                                   (cancel, exit, disable)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from broker.broker_interface import Broker
from broker.charges import ChargeCalculator
from broker.paper_broker import PaperBroker
from config.settings import BotConfig
from core.clock import now_ist
from core.enums import RiskSeverity, StrategyStatus
from data.market_data import MarketDataService
from data.storage.repositories import Repositories
from execution.operator_controls import OperatorControls
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from execution.reconciliation import Reconciler
from execution.square_off import SquareOffManager
from monitoring.alerts import Alert, AlertSink, FanoutAlertSink, LogAlertSink, MemoryAlertSink
from risk.daily_loss_guard import DailyLossGuard
from risk.kill_switch import KillReason, KillSwitch
from risk.risk_manager import RiskManager


@dataclass
class ExecutionStack:
    cfg: BotConfig
    repos: Repositories
    broker: Broker
    market_data: MarketDataService
    alerts: MemoryAlertSink
    kill_switch: KillSwitch
    guard: DailyLossGuard
    orders: OrderManager
    positions: PositionManager
    risk: RiskManager
    reconciler: Reconciler
    square_off: SquareOffManager
    notify: AlertSink | None = None  # fan-out to every sink (memory, log, Telegram)
    controls: OperatorControls | None = None  # named-operator actions (dashboard)

    async def settle(self) -> None:
        """Wait for background tasks (exits, kill handlers, reconciliation)."""
        for _ in range(5):
            await self.positions.wait_idle()
            await self.kill_switch.wait_idle()
            await self.reconciler.wait_pending()
            await asyncio.sleep(0)


def build_stack(
    cfg: BotConfig,
    *,
    repos: Repositories,
    broker: Broker,
    market_data: MarketDataService,
    alert_sinks: tuple[AlertSink, ...] = (),
    now_fn: Callable[[], datetime] = now_ist,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    reconcile_confirm_delay: float = 1.0,
) -> ExecutionStack:
    if broker.mode is not cfg.mode or repos.mode is not cfg.mode:
        raise ValueError("broker, repositories and config must share one trading mode")
    # Every order, fill, position, trade, risk-event and strategy timestamp is
    # written with the stack's clock, never the wall clock (backtests).
    repos = repos.with_clock(now_fn)
    memory = MemoryAlertSink()
    alerts = FanoutAlertSink(memory, LogAlertSink(), *alert_sinks)
    ks_cfg = cfg.risk.kill_switch

    kill_switch = KillSwitch(mode=cfg.mode, repo=repos.risk_events, alerts=alerts, now_fn=now_fn)
    strategy_limits = {sid: s.risk for sid, s in cfg.strategies.strategies.items()}

    def on_strategy_breach(sid: str, loss: float) -> None:
        kill_switch.trigger(KillReason.STRATEGY_DAILY_LOSS, f"daily loss {loss:.2f} reached limit", strategy_id=sid)

    def on_global_breach(loss: float) -> None:
        kill_switch.trigger(KillReason.GLOBAL_DAILY_LOSS,
                            f"global daily loss {loss:.2f} reached limit {cfg.risk.global_.max_daily_loss:.0f}")

    def on_pause(sid: str, reason: str) -> None:
        repos.strategies.set_status(sid, StrategyStatus.PAUSED, at=now_fn())
        alerts.send(Alert(RiskSeverity.WARNING, "Strategy paused", reason, strategy_id=sid))

    guard = DailyLossGuard(cfg.risk, strategy_limits, mode=cfg.mode, repo=repos.daily_pnl, now_fn=now_fn,
                           on_strategy_breach=on_strategy_breach, on_global_breach=on_global_breach, on_pause=on_pause)
    charges = ChargeCalculator(cfg.settings.costs)
    orders = OrderManager(broker=broker, repos=repos, kill_switch=kill_switch, settings=cfg.settings.execution,
                          max_consecutive_rejections=ks_cfg.max_consecutive_order_rejections, alerts=alerts,
                          now_fn=now_fn, sleep=sleep, charges=charges)
    positions = PositionManager(cfg, repos=repos, orders=orders, guard=guard, now_fn=now_fn)

    def status_of(sid: str) -> StrategyStatus:
        rec = repos.strategies.get(sid)
        return rec.status if rec else StrategyStatus.DISABLED

    risk = RiskManager(cfg, guard=guard, kill_switch=kill_switch, trades=positions, quote_fn=market_data.get,
                       is_fresh_fn=market_data.is_fresh, strategy_status_fn=status_of,
                       cost_fn=lambda q, e, x: charges.round_trip(q, e, x), now_fn=now_fn)
    positions.on_tick_checked = risk.check_spread
    reconciler = Reconciler(broker=broker, repos=repos, orders=orders, positions=positions, kill_switch=kill_switch,
                            api_result=risk.record_api_result,
                            debounce_seconds=cfg.settings.execution.reconcile_debounce_seconds,
                            interval_seconds=cfg.settings.execution.reconcile_interval_seconds,
                            confirm_delay=reconcile_confirm_delay, now_fn=now_fn, sleep=sleep)
    square_off = SquareOffManager(session=cfg.settings.market_session, rules=cfg.risk.time_rules, repos=repos,
                                  orders=orders, positions=positions, reconciler=reconciler, kill_switch=kill_switch,
                                  alerts=alerts, now_fn=now_fn)

    # Tick routing: the paper broker fills resting orders first, then local monitoring.
    if isinstance(broker, PaperBroker):
        market_data.add_listener(broker.on_tick)
    market_data.add_listener(positions.on_tick)

    controls = OperatorControls(cfg, repos=repos, kill_switch=kill_switch, guard=guard, positions=positions,
                                reconciler=reconciler, alerts=alerts, now_fn=now_fn)
    return ExecutionStack(cfg, repos, broker, market_data, memory, kill_switch, guard, orders, positions, risk,
                          reconciler, square_off, notify=alerts, controls=controls)
