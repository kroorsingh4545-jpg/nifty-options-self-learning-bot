"""Alert sinks.

Every alert goes through the execution stack's fan-out: the in-memory feed
(dashboard), the JSON log, and Telegram when it is configured
(``monitoring.telegram``). ``AlertLogHandler`` turns ERROR / CRITICAL log
records into ERROR alerts, so failures that never raise an alert themselves
(a scheduled job, a protective stop that could not be placed, ...) still
reach the operator."""

from __future__ import annotations

import logging
import threading
import time as wall_time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from core.clock import now_ist
from core.enums import RiskSeverity
from monitoring.json_logger import get_logger

log = get_logger("alerts")


class AlertKind(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ERROR = "ERROR"
    DAILY_LOSS = "DAILY_LOSS"
    KILL_SWITCH = "KILL_SWITCH"
    EOD_SUMMARY = "EOD_SUMMARY"
    GENERAL = "GENERAL"


@dataclass(frozen=True)
class Alert:
    severity: RiskSeverity
    title: str
    message: str
    strategy_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=now_ist)
    kind: AlertKind = AlertKind.GENERAL


class AlertSink(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogAlertSink:
    def send(self, alert: Alert) -> None:
        level = {"INFO": log.info, "WARNING": log.warning, "CRITICAL": log.critical}[alert.severity.value]
        level(f"ALERT: {alert.title}", extra={"strategy_id": alert.strategy_id, "alert": alert.message,
                                               "kind": alert.kind.value, **alert.details})


class MemoryAlertSink:
    """Keeps the most recent alerts (dashboard feed / tests)."""

    def __init__(self, maxlen: int = 500) -> None:
        self.alerts: deque[Alert] = deque(maxlen=maxlen)

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class FanoutAlertSink:
    def __init__(self, *sinks: AlertSink) -> None:
        self.sinks = list(sinks)

    def send(self, alert: Alert) -> None:
        for s in self.sinks:
            try:
                s.send(alert)
            except Exception:  # an alert channel failing must never break trading safety paths
                log.exception("alert sink failed")


#: Loggers never turned into alerts: the alert log itself (it would loop), the
#: alert channel's own errors, HTTP client internals, and the kill switch
#: (which alerts on its own).
EXCLUDED_LOGGERS = ("alerts", "monitoring.telegram", "httpx", "httpcore", "risk.kill_switch")


class AlertLogHandler(logging.Handler):
    """ERROR / CRITICAL log records -> ERROR alerts, de-duplicated by
    (logger, message) within ``dedupe_seconds``. Never raises."""

    def __init__(self, sink: AlertSink, *, dedupe_seconds: float = 300.0, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self.sink = sink
        self.dedupe_seconds = dedupe_seconds
        self._seen: dict[tuple[str, str], float] = {}
        self._guard = threading.local()
        self._lock = threading.Lock()

    def _excluded(self, name: str) -> bool:
        return any(name == x or name.startswith(x + ".") for x in EXCLUDED_LOGGERS)

    def emit(self, record: logging.LogRecord) -> None:
        if self._excluded(record.name) or getattr(self._guard, "active", False):
            return
        try:
            msg = record.getMessage()
            key = (record.name, msg)
            now = wall_time.monotonic()
            with self._lock:
                last = self._seen.get(key)
                if last is not None and now - last < self.dedupe_seconds:
                    return
                self._seen[key] = now
                if len(self._seen) > 1000:
                    self._seen = {k: v for k, v in self._seen.items() if now - v < self.dedupe_seconds}
            detail = f"{record.levelname} in {record.name}"
            if record.exc_info and record.exc_info[0] is not None:
                detail += f": {record.exc_info[0].__name__}: {record.exc_info[1]}"
            sev = RiskSeverity.CRITICAL if record.levelno >= logging.CRITICAL else RiskSeverity.WARNING
            sid = getattr(record, "strategy_id", None)
            self._guard.active = True
            try:
                self.sink.send(Alert(sev, msg[:200], detail[:1000], strategy_id=sid if isinstance(sid, str) else None,
                                     kind=AlertKind.ERROR))
            finally:
                self._guard.active = False
        except Exception:  # pragma: no cover - logging must never fail the caller
            self.handleError(record)
