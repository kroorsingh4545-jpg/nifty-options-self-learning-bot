"""Operator actions requested by a named human (via the dashboard or the
Telegram menu; both queue the same commands).

The dashboard never acts on its own: it queues a command, and the bot runs it
here, through the same risk and execution services the strategies use.

Kill-switch reset by a named human:

1. The kill's own actions (cancel, flatten, mark strategies KILLED) must have
   finished, so the reset never races them; then the scope must still be
   blocked.
2. A daily-loss kill from today cannot be reset: the daily loss limit is a
   hard limit, and that kill clears automatically on the next trading day.
3. Reconciliation must pass first, so local state and the broker agree. A
   mismatch refuses the reset (and keeps or re-triggers the kill switch).
4. The kill switch is reset with the operator's name and note (audited).
5. Every KILLED strategy in the scope is explicitly re-activated, unless it
   is still blocked (another kill scope, today's daily-loss breach, or
   disabled in config). Each re-activation is recorded as a risk event with
   the operator's name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config.settings import BotConfig
from core.clock import now_ist
from core.enums import RiskSeverity, StrategyStatus, TradeStatus
from data.storage.records import RiskEventRecord
from data.storage.repositories import Repositories
from execution.position_manager import PositionManager
from execution.reconciliation import Reconciler
from monitoring.alerts import Alert, AlertSink, LogAlertSink
from monitoring.json_logger import get_logger
from risk.daily_loss_guard import DailyLossGuard
from risk.kill_switch import KillReason, KillSwitch
from risk.stop_loss_manager import ExitReason

log = get_logger(__name__)

REACTIVATED_EVENT = "STRATEGY_REACTIVATED"
OPERATOR_EXIT_EVENT = "OPERATOR_EXIT"


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _named(operator: str) -> str:
    if not operator or not operator.strip():
        raise ValueError("a named operator is required")
    return operator.strip()


class OperatorControls:
    def __init__(
        self,
        cfg: BotConfig,
        *,
        repos: Repositories,
        kill_switch: KillSwitch,
        guard: DailyLossGuard,
        positions: PositionManager,
        reconciler: Reconciler,
        alerts: AlertSink | None = None,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.cfg = cfg
        self.repos = repos
        self.kill_switch = kill_switch
        self.guard = guard
        self.positions = positions
        self.reconciler = reconciler
        self.alerts = alerts or LogAlertSink()
        self._now = now_fn

    # ------------------------------------------------------------------ #
    def trigger_kill(self, operator: str, note: str, strategy_id: str | None = None, *,
                     source: str = "dashboard") -> ControlResult:
        op = _named(operator)
        if strategy_id is not None and strategy_id not in self.cfg.strategies.strategies:
            return ControlResult(False, f"unknown strategy {strategy_id!r}")
        where = source if source in ("dashboard", "telegram") else "dashboard"
        ev = self.kill_switch.trigger(KillReason.MANUAL, f"{where} ({op}): {note or 'emergency stop'}",
                                      strategy_id=strategy_id)
        scope = strategy_id or "GLOBAL"
        if ev is None:
            return ControlResult(True, f"kill switch already active for {scope}")
        return ControlResult(True, f"kill switch triggered for {scope}: new entries blocked, positions exiting")

    # ------------------------------------------------------------------ #
    async def reset_kill_switch(self, operator: str, note: str, strategy_id: str | None = None) -> ControlResult:
        op = _named(operator)
        if not note or not note.strip():
            return ControlResult(False, "a note explaining the reset is required")
        ks = self.kill_switch
        scope = strategy_id or "GLOBAL"
        # Let the kill's own actions (cancel, flatten, mark strategies KILLED)
        # finish first, so the reset never races the action it undoes. Bounded
        # by the exit retries; a failed exit escalates the kill meanwhile.
        await ks.wait_idle()
        ev = ks.global_event if strategy_id is None else ks.strategy_events.get(strategy_id)
        if ev is None:
            return ControlResult(False, f"kill switch is not active for {scope}")
        today = self._now().date()
        if ev.day_scoped and not ev.expired_on(today):
            return ControlResult(False, f"{ev.reason.value} cannot be reset on the day it happened: the daily loss "
                                        "limit is a hard limit; it clears automatically on the next trading day")
        report = await self.reconciler.reconcile("operator_reset")
        if not report.ok:
            if not report.reachable:
                why = "broker unreachable"
            else:
                why = "; ".join(f"{m.kind} {m.key} local={m.local} broker={m.broker}" for m in report.mismatches)
            return ControlResult(False, f"reset refused: reconciliation failed ({why}). Resolve it first.",
                                 {"mismatches": len(report.mismatches)})
        if not ks.reset(operator=op, note=note.strip(), strategy_id=strategy_id):
            return ControlResult(False, f"kill switch is not active for {scope}")
        reactivated, kept = self._reactivate(op, strategy_id)
        msg = f"kill switch reset for {scope} by {op}; re-activated: {', '.join(reactivated) or 'none'}"
        if kept:
            msg += "; still blocked: " + "; ".join(f"{k} ({v})" for k, v in kept.items())
        self.alerts.send(Alert(RiskSeverity.WARNING, f"Kill switch reset ({scope})", msg, strategy_id=strategy_id))
        log.warning("operator kill switch reset", extra={"strategy_id": strategy_id, "operator": op,
                                                         "reactivated": reactivated, "kept": kept})
        return ControlResult(True, msg, {"reactivated": reactivated, "still_blocked": kept})

    def _blocked_reason(self, sid: str) -> str | None:
        scfg = self.cfg.strategies.strategies.get(sid)
        if scfg is None or not scfg.enabled:
            return "disabled in config"
        if self.kill_switch.is_blocked(sid):
            return self.kill_switch.block_reason(sid) or "kill switch active"
        if self.guard.global_breached:
            return "global daily loss limit breached today"
        day = self.guard.state.get(sid)
        if day is not None and day.breached:
            return "strategy daily loss limit breached today"
        return None

    def _reactivate(self, operator: str, strategy_id: str | None) -> tuple[list[str], dict[str, str]]:
        reactivated: list[str] = []
        kept: dict[str, str] = {}
        now = self._now()
        for rec in self.repos.strategies.list_all():
            if strategy_id is not None and rec.strategy_id != strategy_id:
                continue
            if rec.status is not StrategyStatus.KILLED:
                continue
            blocked = self._blocked_reason(rec.strategy_id)
            if blocked:
                kept[rec.strategy_id] = blocked
                continue
            self.repos.strategies.set_status(rec.strategy_id, StrategyStatus.ACTIVE, at=now)
            self.repos.risk_events.insert(RiskEventRecord(
                mode=self.repos.mode, strategy_id=rec.strategy_id, event_type=REACTIVATED_EVENT,
                severity=RiskSeverity.WARNING, message=f"re-activated by {operator} after kill switch reset",
                details_json={"operator": operator}, created_at=now))
            reactivated.append(rec.strategy_id)
        return reactivated, kept

    # ------------------------------------------------------------------ #
    async def exit_position(self, operator: str, trade_uid: str, note: str = "") -> ControlResult:
        op = _named(operator)
        trade = self.positions.trades.get(trade_uid)
        if trade is None or trade.status is not TradeStatus.OPEN:
            return ControlResult(False, f"no open trade {trade_uid}")
        now = self._now()
        self.repos.risk_events.insert(RiskEventRecord(
            mode=self.repos.mode, strategy_id=trade.strategy_id, event_type=OPERATOR_EXIT_EVENT,
            severity=RiskSeverity.WARNING, message=f"manual exit of {trade.instrument.trading_symbol} by {op}",
            details_json={"operator": op, "trade_uid": trade_uid, "note": note}, created_at=now))
        ok = await self.positions.exit_trade(trade_uid, ExitReason.MANUAL)
        if ok:
            return ControlResult(True, f"exited {trade.instrument.trading_symbol}")
        return ControlResult(False, f"exit of {trade.instrument.trading_symbol} did not complete: "
                                    "see alerts (the kill switch escalates a failed exit)")
