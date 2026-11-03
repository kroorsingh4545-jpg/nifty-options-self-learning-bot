"""Kill switch — global or per strategy.

When triggered:
1. New ENTRY orders are blocked immediately (synchronously).
2. A CRITICAL risk event is written to the append-only ``risk_events`` table.
3. Alerts are sent (dashboard memory sink; Telegram from Phase 10).
4. Registered async action handlers run (execution: cancel pending orders,
   exit positions, disable strategies).

It stays active for the rest of the day unless a named human resets it.
ML/RL/learning components receive no reference to this object; only risk,
execution and the dashboard (manual trigger) hold it.

After a restart, ``restore()`` re-arms any trigger recorded today that was not
followed by a reset.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from core.clock import now_ist
from core.enums import RiskSeverity, TradingMode
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


@dataclass(frozen=True)
class KillEvent:
    reason: KillReason
    message: str
    strategy_id: str | None  # None = global
    at: datetime


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
        """Activate (idempotent per scope). Returns the event, or None if that
        scope was already active."""
        if strategy_id is None and self.global_event is not None:
            return None
        if strategy_id is not None and (strategy_id in self.strategy_events or self.global_event is not None):
            return None
        ev = KillEvent(reason=KillReason(reason), message=message, strategy_id=strategy_id, at=self._now())
        if strategy_id is None:
            self.global_event = ev
        else:
            self.strategy_events[strategy_id] = ev
        self.history.append(ev)
        scope = strategy_id or "GLOBAL"
        log.critical("KILL SWITCH TRIGGERED", extra={"strategy_id": strategy_id, "scope": scope,
                                                     "reason": ev.reason.value, "detail": message})
        self._record(TRIGGER_EVENT, RiskSeverity.CRITICAL, ev.reason.value, message, strategy_id)
        self.alerts.send(Alert(RiskSeverity.CRITICAL, f"Kill switch triggered ({scope})",
                               f"{ev.reason.value}: {message}", strategy_id=strategy_id))
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
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

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
                                             details_json={"reason": reason, **(extra or {})}))
        except Exception:
            # Persistence failure must not stop the kill switch from acting.
            log.exception("failed to persist kill switch event")

    def restore(self) -> None:
        """Re-activate scopes triggered today and not reset since (restart recovery)."""
        if self.repo is None:
            return
        today = self._now().date()
        events = [e for e in reversed(self.repo.recent(limit=1000)) if e.created_at.date() == today
                  and e.event_type in (TRIGGER_EVENT, RESET_EVENT)]
        state: dict[str | None, RiskEventRecord | None] = {}
        for e in events:
            state[e.strategy_id] = e if e.event_type == TRIGGER_EVENT else None
        for sid, last in state.items():
            if last is None:
                continue
            ev = KillEvent(KillReason(last.details_json.get("reason", "MANUAL")), f"restored: {last.message}", sid,
                           last.created_at)
            if sid is None:
                self.global_event = ev
            else:
                self.strategy_events[sid] = ev
            log.critical("kill switch restored as ACTIVE after restart", extra={"strategy_id": sid})

    def status(self) -> dict[str, object]:
        return {
            "armed": self.armed,
            "global_active": self.active,
            "global_reason": self.global_event.reason.value if self.global_event else None,
            "strategies": {sid: ev.reason.value for sid, ev in self.strategy_events.items()},
        }
