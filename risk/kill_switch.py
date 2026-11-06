"""Kill switch — global or per strategy.

When triggered:
1. New ENTRY orders are blocked immediately (synchronously).
2. A CRITICAL risk event is written to the append-only ``risk_events`` table.
3. Alerts are sent (dashboard memory sink; Telegram from Phase 10).
4. Registered async action handlers run (execution: cancel pending orders,
   exit positions, disable strategies).

How long it stays active depends on the reason:

* Daily-loss reasons (``DAY_SCOPED_REASONS``) mean "disabled for the day": they
  are cleared automatically by ``roll_day`` on the next trading day (audited).
* Every other reason (exit failure, incomplete square-off, reconciliation
  mismatch, API / auth failure, manual, ...) stays active, across days and
  restarts, until a named human resets it.

A persistent reason arriving while only a day-scoped kill is active for that
scope is still recorded (escalation), so it cannot be lost when the day-scoped
kill expires.

ML/RL/learning components receive no reference to this object; only risk,
execution and the dashboard (manual trigger) hold it.

After a restart, ``restore()`` re-arms every trigger not followed by a reset,
except day-scoped ones from previous days.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from core.clock import now_ist
from core.enums import RiskSeverity, TradingMode
from core.tasks import wait_for_tasks
from data.storage.records import RiskEventRecord
from data.storage.repositories import RiskEventRepository
from monitoring.alerts import Alert, AlertSink, LogAlertSink
from monitoring.json_logger import get_logger

log = get_logger(__name__)

TRIGGER_EVENT = "KILL_SWITCH_TRIGGERED"
RESET_EVENT = "KILL_SWITCH_RESET"


class KillReason(StrEnum):
    STRATEGY_DAILY_LOSS = "STRATEGY_DAILY_LOSS"
    GLOBAL_DAILY_LOSS = "GLOBAL_DAILY_LOSS"
    API_FAILURE = "API_FAILURE"
    RATE_LIMIT_CIRCUIT = "RATE_LIMIT_CIRCUIT"
    AUTH_FAILURE = "AUTH_FAILURE"
    WEBSOCKET_DISCONNECT = "WEBSOCKET_DISCONNECT"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    ABNORMAL_SPREAD = "ABNORMAL_SPREAD"
    REPEATED_REJECTIONS = "REPEATED_REJECTIONS"
    EXIT_FAILURE = "EXIT_FAILURE"
    SQUARE_OFF_INCOMPLETE = "SQUARE_OFF_INCOMPLETE"
    MANUAL = "MANUAL"


# Cleared automatically on the next trading day. Consecutive losses never use
# the kill switch: they PAUSE the strategy (strategy status), which the bot's
# new-day reset clears.
DAY_SCOPED_REASONS: frozenset[KillReason] = frozenset({KillReason.STRATEGY_DAILY_LOSS,
                                                       KillReason.GLOBAL_DAILY_LOSS})


@dataclass(frozen=True)
class KillEvent:
    reason: KillReason
    message: str
    strategy_id: str | None  # None = global
    at: datetime

    @property
    def day_scoped(self) -> bool:
        return self.reason in DAY_SCOPED_REASONS

    def expired_on(self, today: date) -> bool:
        return self.day_scoped and self.at.date() < today


# handler(event) -> awaitable; e.g. flatten positions for the scope.
KillHandler = Callable[[KillEvent], Awaitable[None]]


class KillSwitch:
    def __init__(
        self,
        *,
        mode: TradingMode = TradingMode.PAPER,
        repo: RiskEventRepository | None = None,
        alerts: AlertSink | None = None,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.mode = mode
        self.repo = repo
        self.alerts = alerts or LogAlertSink()
        self._now = now_fn
        self.global_event: KillEvent | None = None
        self.strategy_events: dict[str, KillEvent] = {}
        self.history: list[KillEvent] = []
        self._handlers: list[KillHandler] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.armed = True

    # ------------------------------------------------------------------ #
    def add_handler(self, handler: KillHandler) -> None:
        self._handlers.append(handler)

    @property
    def active(self) -> bool:
        return self.global_event is not None

    def is_blocked(self, strategy_id: str | None = None) -> bool:
        """True if new entries are blocked for this strategy (or globally)."""
        if self.global_event is not None:
            return True
        return strategy_id is not None and strategy_id in self.strategy_events

    def block_reason(self, strategy_id: str | None = None) -> str | None:
        ev = self.global_event or (self.strategy_events.get(strategy_id) if strategy_id else None)
        return f"kill switch active ({ev.reason.value}): {ev.message}" if ev else None

    # ------------------------------------------------------------------ #
    def trigger(self, reason: KillReason, message: str, *, strategy_id: str | None = None) -> KillEvent | None:
        """Activate (idempotent per scope). Returns the event, or None if the
        scope was already covered by an equally long-lived kill.

        A persistent reason arriving while the scope is blocked only by a
        day-scoped kill is recorded as an escalation: it replaces the
        day-scoped event so it survives the next day's roll-over. Escalations
        do not re-run the action handlers (the scope is already flattened)."""
        reason = KillReason(reason)
        persistent = reason not in DAY_SCOPED_REASONS
        current = self.global_event if strategy_id is None else self.strategy_events.get(strategy_id)
        if current is not None and not (persistent and current.day_scoped):
            return None
        g = self.global_event
        if strategy_id is not None and g is not None and not (persistent and g.day_scoped):
            return None
        escalation = self.is_blocked(strategy_id)
        ev = KillEvent(reason=reason, message=message, strategy_id=strategy_id, at=self._now())
        if strategy_id is None:
            self.global_event = ev
        else:
            self.strategy_events[strategy_id] = ev
        self.history.append(ev)
        scope = strategy_id or "GLOBAL"
        title = "KILL SWITCH ESCALATED" if escalation else "KILL SWITCH TRIGGERED"
        log.critical(title, extra={"strategy_id": strategy_id, "scope": scope, "reason": ev.reason.value,
                                   "detail": message, "persistent": persistent})
        self._record(TRIGGER_EVENT, RiskSeverity.CRITICAL, ev.reason.value, message, strategy_id,
                     {"escalation": "true"} if escalation else None)
        until = "until a named human resets it" if persistent else "for the rest of the trading day"
        self.alerts.send(Alert(RiskSeverity.CRITICAL, f"Kill switch {'escalated' if escalation else 'triggered'} "
                               f"({scope})", f"{ev.reason.value}: {message} - active {until}",
                               strategy_id=strategy_id))
        if not escalation:
            self._run_handlers(ev)
        return ev

    def _run_handlers(self, ev: KillEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.error("kill switch handlers not run: no running event loop")
            return
        for h in self._handlers:
            task = loop.create_task(self._safe(h, ev))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe(self, handler: KillHandler, ev: KillEvent) -> None:
        try:
            await handler(ev)
        except Exception:
            log.exception("kill switch handler failed", extra={"strategy_id": ev.strategy_id})
            self.alerts.send(Alert(RiskSeverity.CRITICAL, "Kill switch action FAILED",
                                   "Manual intervention required: verify positions at the broker.",
                                   strategy_id=ev.strategy_id))

    async def wait_idle(self) -> None:
        """Await all running handler tasks (tests / orderly shutdown)."""
        await wait_for_tasks(self._tasks)

    # ------------------------------------------------------------------ #
    def reset(self, *, operator: str, note: str, strategy_id: str | None = None) -> bool:
        """Manual reset by a named human. Returns True if something was reset."""
        if not operator or not operator.strip():
            raise ValueError("a named operator is required to reset the kill switch")
        if strategy_id is None:
            if self.global_event is None:
                return False
            self.global_event = None
        else:
            if self.strategy_events.pop(strategy_id, None) is None:
                return False
        log.warning("kill switch reset", extra={"strategy_id": strategy_id, "operator": operator, "note": note})
        self._record(RESET_EVENT, RiskSeverity.WARNING, "MANUAL_RESET", f"reset by {operator}: {note}", strategy_id,
                     {"operator": operator})
        return True

    def _record(self, event_type: str, sev: RiskSeverity, reason: str, message: str, strategy_id: str | None,
                extra: dict[str, str] | None = None) -> None:
        if self.repo is None:
            return
        try:
            self.repo.insert(RiskEventRecord(mode=self.mode, strategy_id=strategy_id, event_type=event_type,
                                             severity=sev, message=message,
                                             details_json={"reason": reason, **(extra or {})},
                                             # restore()/roll_day() read events back by this clock's date.
                                             created_at=self._now()))
        except Exception:
            # Persistence failure must not stop the kill switch from acting.
            log.exception("failed to persist kill switch event")

    def restore(self) -> None:
        """Re-activate every scope whose last kill event is a trigger (restart
        recovery), on any day, except day-scoped triggers from previous days."""
        if self.repo is None:
            return
        today = self._now().date()
        state: dict[str | None, RiskEventRecord | None] = {}
        for e in self.repo.list_by_types((TRIGGER_EVENT, RESET_EVENT)):
            state[e.strategy_id] = e if e.event_type == TRIGGER_EVENT else None
        for sid, last in state.items():
            if last is None:
                continue
            try:
                reason = KillReason(last.details_json.get("reason", "MANUAL"))
            except ValueError:
                reason = KillReason.MANUAL  # unknown reason: treat as persistent
            ev = KillEvent(reason, f"restored: {last.message}", sid, last.created_at)
            if ev.expired_on(today):
                continue
            if sid is None:
                self.global_event = ev
            else:
                self.strategy_events[sid] = ev
            log.critical("kill switch restored as ACTIVE after restart",
                         extra={"strategy_id": sid, "reason": reason.value, "since": last.created_at.isoformat()})

    def roll_day(self, today: date) -> list[str]:
        """New trading day: clear day-scoped (daily-loss) kill events from
        PREVIOUS days. Every other reason stays active until a named human
        resets it. Returns the scopes cleared; each is audited."""
        cleared: list[str] = []
        if self.global_event is not None and self.global_event.expired_on(today):
            self.global_event = None
            cleared.append("GLOBAL")
        for sid, ev in list(self.strategy_events.items()):
            if ev.expired_on(today):
                del self.strategy_events[sid]
                cleared.append(sid)
        for scope in cleared:
            scope_sid: str | None = None if scope == "GLOBAL" else scope
            log.warning("kill switch cleared for new trading day", extra={"strategy_id": scope_sid,
                                                                          "day": str(today)})
            self._record(RESET_EVENT, RiskSeverity.INFO, "NEW_DAY", f"auto-cleared for trading day {today}",
                         scope_sid, {"operator": "system:new_day"})
        remaining = ([self.global_event] if self.global_event else []) + list(self.strategy_events.values())
        for ev in remaining:
            if ev.at.date() < today:
                scope = ev.strategy_id or "GLOBAL"
                log.critical("kill switch still active on new trading day; a named human must reset it",
                             extra={"strategy_id": ev.strategy_id, "reason": ev.reason.value,
                                    "since": ev.at.isoformat()})
                self.alerts.send(Alert(RiskSeverity.CRITICAL, f"Kill switch still active ({scope})",
                                       f"{ev.reason.value} since {ev.at:%Y-%m-%d %H:%M}: {ev.message} - "
                                       "new entries stay blocked until a named human resets it",
                                       strategy_id=ev.strategy_id))
        return cleared

    def status(self) -> dict[str, object]:
        return {
            "armed": self.armed,
            "global_active": self.active,
            "global_reason": self.global_event.reason.value if self.global_event else None,
            "strategies": {sid: ev.reason.value for sid, ev in self.strategy_events.items()},
        }
