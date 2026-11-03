"""Alert sinks. Telegram delivery is added in Phase 10; until then alerts are
logged and kept in memory for the dashboard."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from core.clock import now_ist
from core.enums import RiskSeverity
from monitoring.json_logger import get_logger

log = get_logger("alerts")


@dataclass(frozen=True)
class Alert:
    severity: RiskSeverity
    title: str
    message: str
    strategy_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=now_ist)


class AlertSink(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogAlertSink:
    def send(self, alert: Alert) -> None:
        level = {"INFO": log.info, "WARNING": log.warning, "CRITICAL": log.critical}[alert.severity.value]
        level(f"ALERT: {alert.title}", extra={"strategy_id": alert.strategy_id, "alert": alert.message,
                                               **alert.details})


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
