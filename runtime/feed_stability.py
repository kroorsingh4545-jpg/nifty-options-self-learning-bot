"""How long the market-data feed has been connected and stable.

Fed once a second by the control plane (``ControlService.publish_state``) with
the launcher's connection status. *Stable* means: WebSocket connected, not
stale, and no reconnect since the stable period began. Readings older than
``max_sample_age_seconds`` count as unknown (not stable), so a stopped
heartbeat can never look like a healthy feed.

Used by the Telegram menu: a kill-switch reset from Telegram is refused unless
the feed has been stable for ``stable_seconds`` right now (re-checked when the
bot runs the command), and the "feed is back" message after a
WEBSOCKET_DISCONNECT kill waits for the same period. It never resets anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


class FeedStability:
    def __init__(self, stable_seconds: float = 120.0, max_sample_age_seconds: float = 10.0) -> None:
        self.stable_seconds = float(stable_seconds)
        self.max_sample_age = float(max_sample_age_seconds)
        self.since: datetime | None = None
        self.last_seen: datetime | None = None
        self.last_state: str = "unknown"
        self._reconnects: Any = None

    def observe(self, status: Mapping[str, Any], now: datetime) -> None:
        connected = bool(status.get("websocket_connected")) and not bool(status.get("websocket_stale"))
        reconnects = status.get("reconnects")
        self.last_state = str(status.get("websocket_state") or ("CONNECTED" if connected else "DISCONNECTED"))
        if status.get("websocket_stale"):
            self.last_state += " (stale)"
        if not connected:
            self.since = None
        elif self.since is None or (reconnects is not None and self._reconnects is not None
                                    and reconnects != self._reconnects):
            self.since = now  # a new stable period starts at the (re)connection
        self._reconnects = reconnects
        self.last_seen = now

    def stable_for(self, now: datetime) -> float | None:
        """Seconds of continuous stable feed, or None when not stable / unknown."""
        if self.last_seen is None or (now - self.last_seen).total_seconds() > self.max_sample_age:
            return None
        if self.since is None:
            return None
        return max(0.0, (now - self.since).total_seconds())

    def check(self, now: datetime) -> tuple[bool, str]:
        """(ok, plain-words reason) for "connected and stable for stable_seconds"."""
        if self.last_seen is None or (now - self.last_seen).total_seconds() > self.max_sample_age:
            return False, "feed state unknown (no recent reading from the bot)"
        if self.since is None:
            return False, f"feed not connected ({self.last_state})"
        held = (now - self.since).total_seconds()
        if held < self.stable_seconds:
            return False, (f"feed connected but stable for only {held:.0f}s "
                           f"(needs {self.stable_seconds:.0f}s without a reconnect)")
        return True, f"feed connected and stable for {held:.0f}s"

    def snapshot(self, now: datetime) -> dict[str, Any]:
        ok, why = self.check(now)
        held = self.stable_for(now)
        return {"stable": ok, "stable_for_seconds": round(held, 1) if held is not None else None,
                "required_seconds": self.stable_seconds, "detail": why}


__all__ = ["FeedStability"]
