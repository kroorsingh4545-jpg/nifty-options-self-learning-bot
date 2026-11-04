"""Session phases, mandatory square-off, and kill-switch flattening.

Square-off (15:10–15:20 IST by default):
1. reconcile ("pre_square_off")
2. cancel every working order (entries, protective stops)
3. exit every open position at market (safe retries)
4. reconcile ("post_square_off")
Repeated every check while anything is still open inside the window. If a
position is still open at ``square_off_end`` the kill switch fires and a
CRITICAL alert asks for manual intervention.

Kill switch handler: cancel pending orders, exit positions and disable the
affected strategy (or ALL strategies for a global trigger) for the day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from config.settings import MarketSession, TimeRules
from core.clock import now_ist
from core.enums import RiskSeverity, StrategyStatus
from data.storage.repositories import Repositories
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from execution.reconciliation import Reconciler
from monitoring.alerts import Alert, AlertSink, LogAlertSink
from monitoring.json_logger import get_logger
from risk.kill_switch import KillEvent, KillReason, KillSwitch
from risk.stop_loss_manager import ExitReason

log = get_logger(__name__)


class SessionPhase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    TRADING = "TRADING"
    NO_NEW_ENTRIES = "NO_NEW_ENTRIES"
    SQUARE_OFF = "SQUARE_OFF"
    CLOSED = "CLOSED"


def session_phase(now: datetime, session: MarketSession, rules: TimeRules) -> SessionPhase:
    t = now.time()
    if t < session.market_open:
        return SessionPhase.PRE_OPEN
    if t >= rules.square_off_end or t >= session.market_close:
        return SessionPhase.CLOSED
    if t >= rules.square_off_start:
        return SessionPhase.SQUARE_OFF
    if t >= rules.no_new_entries_after:
        return SessionPhase.NO_NEW_ENTRIES
    return SessionPhase.TRADING


class SquareOffManager:
    def __init__(
        self,
        *,
        session: MarketSession,
        rules: TimeRules,
        repos: Repositories,
        orders: OrderManager,
        positions: PositionManager,
        reconciler: Reconciler,
        kill_switch: KillSwitch,
        alerts: AlertSink | None = None,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.session = session
        self.rules = rules
        self.repos = repos
        self.orders = orders
        self.positions = positions
        self.reconciler = reconciler
        self.kill_switch = kill_switch
        self.alerts = alerts or LogAlertSink()
        self._now = now_fn
        self.started_for: object = None
        self.completed_for: object = None
        self.escalated_for: object = None
        kill_switch.add_handler(self.on_kill)

    def phase(self, now: datetime | None = None) -> SessionPhase:
        return session_phase(now or self._now(), self.session, self.rules)

    def seconds_to_square_off(self, now: datetime | None = None) -> float:
        n = now or self._now()
        target = n.replace(hour=self.rules.square_off_start.hour, minute=self.rules.square_off_start.minute,
                           second=0, microsecond=0)
        return max(0.0, (target - n).total_seconds())

    # ------------------------------------------------------------------ #
    async def flatten(self, *, strategy_id: str | None, reason: ExitReason) -> bool:
        """Cancel working orders and exit open positions for the scope."""
        await self.orders.cancel_all(strategy_id)
        ok = await self.positions.exit_all(reason, strategy_id)
        await self.positions.wait_idle()
        flat = self.positions.open_trade_count(strategy_id) == 0 or all(
            t.open_qty == 0 for t in self.positions.open_trades(strategy_id))
        return ok and flat

    async def check(self, now: datetime | None = None) -> SessionPhase:
        """Call periodically (every ``square_off_check_seconds``)."""
        now = now or self._now()
        phase = self.phase(now)
        day = now.date()
        if phase is SessionPhase.SQUARE_OFF and self.completed_for != day:
            if self.started_for != day:
                self.started_for = day
                log.warning("mandatory square-off started", extra={"open_trades": self.positions.open_trade_count()})
                self.alerts.send(Alert(RiskSeverity.INFO, "Square-off started",
                                       f"{self.positions.open_trade_count()} open trade(s)"))
                await self.reconciler.reconcile("pre_square_off")
            flat = await self.flatten(strategy_id=None, reason=ExitReason.SQUARE_OFF)
            await self.reconciler.reconcile("post_square_off")
            if flat and self.positions.net_quantities() == {}:
                self.completed_for = day
                log.info("square-off complete")
        elif phase is SessionPhase.CLOSED and self.started_for == day and self.completed_for != day \
                and self.escalated_for != day and self.positions.net_quantities():
            self.escalated_for = day
            msg = f"positions still open after {self.rules.square_off_end:%H:%M}: {self.positions.net_quantities()}"
            self.alerts.send(Alert(RiskSeverity.CRITICAL, "SQUARE-OFF INCOMPLETE",
                                   msg + " - MANUAL INTERVENTION REQUIRED"))
            self.kill_switch.trigger(KillReason.SQUARE_OFF_INCOMPLETE, msg)
        return phase

    # ------------------------------------------------------------------ #
    async def on_kill(self, ev: KillEvent) -> None:
        sid = ev.strategy_id
        if sid is None:
            for s in self.repos.strategies.list_all():
                self.repos.strategies.set_status(s.strategy_id, StrategyStatus.KILLED, at=self._now())
        else:
            self.repos.strategies.set_status(sid, StrategyStatus.KILLED, at=self._now())
        flat = await self.flatten(strategy_id=sid, reason=ExitReason.KILL_SWITCH)
        log.critical("kill switch flatten finished", extra={"strategy_id": sid, "flat": flat})
        if not flat:
            self.alerts.send(Alert(RiskSeverity.CRITICAL, "Kill-switch flatten incomplete",
                                   "Positions remain open - MANUAL INTERVENTION REQUIRED", strategy_id=sid))
