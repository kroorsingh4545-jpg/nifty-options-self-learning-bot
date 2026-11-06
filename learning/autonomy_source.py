"""Where the learning loop reads the autonomy level from.

The loop reads the level again at every promotion step (``autonomy_fn``), so
it must come from the running bot, not from a value fixed at start-up: a
dashboard switch to OBSERVE_ONLY has to stop the next PAPER_READY decision
or deployment at once.

* In the bot's own process, ``runtime.learning_loop`` passes
  ``lambda: bot.mode.autonomy`` (the live in-memory level the dashboard
  command changes).
* In another process (``python -m learning run``), ``PublishedAutonomy``
  reads the level the running bot publishes every second in
  ``runtime_state``. A missing, stale, non-PAPER or unreadable heartbeat
  falls back to ``persisted_autonomy`` (the bot is not running, so its level
  is the one it would start with).

``persisted_autonomy`` is the level a bot starts with: the most restrictive
of ``app.autonomy_level`` in YAML and the last autonomy change the dashboard
applied. A dashboard switch to OBSERVE_ONLY therefore survives a restart,
and YAML stays the ceiling (the dashboard can raise the level only for the
running session). Anything unreadable resolves to OBSERVE_ONLY.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from config.settings import BotConfig
from core.autonomy import most_restrictive
from core.clock import now_ist
from core.enums import AutonomyLevel, ControlCommandType, TradingMode
from data.storage.repositories import Repositories

log = logging.getLogger(__name__)

FAIL_SAFE = AutonomyLevel.OBSERVE_ONLY


def last_dashboard_level(repos: Repositories) -> AutonomyLevel | None:
    """The level of the last SET_AUTONOMY command the bot applied (DONE)."""
    cmd = repos.commands.last_completed(ControlCommandType.SET_AUTONOMY)
    if cmd is None:
        return None
    try:
        return AutonomyLevel(str(cmd.payload_json.get("level", "")))
    except ValueError:
        return FAIL_SAFE


def persisted_autonomy(repos: Repositories, cfg: BotConfig) -> AutonomyLevel:
    """The level a PAPER bot starts with (see the module docstring)."""
    try:
        yaml_level = AutonomyLevel(cfg.settings.app.autonomy_level)
        last = last_dashboard_level(repos)
    except Exception:
        log.exception("autonomy level unreadable; using OBSERVE_ONLY")
        return FAIL_SAFE
    return yaml_level if last is None else most_restrictive(yaml_level, last)


class PublishedAutonomy:
    """``autonomy_fn`` for a loop in another process: the running bot's level
    from its heartbeat, read on every call."""

    def __init__(self, repos: Repositories, cfg: BotConfig, *, max_age_seconds: float = 10.0,
                 now_fn: Callable[[], datetime] = now_ist) -> None:
        if repos.mode is not TradingMode.PAPER:
            raise ValueError("the learning loop reads the PAPER bot's autonomy level only")
        self.repos = repos
        self.cfg = cfg
        self.max_age_seconds = max_age_seconds
        self._now = now_fn
        self.last_source = ""

    def __call__(self) -> AutonomyLevel:
        try:
            latest = self.repos.runtime_state.latest()
        except Exception:
            log.exception("bot heartbeat unreadable; using OBSERVE_ONLY")
            self.last_source = "unreadable heartbeat"
            return FAIL_SAFE
        if latest is not None:
            state, at = latest
            age = (self._now() - at).total_seconds()
            if age <= self.max_age_seconds and state.get("mode") == TradingMode.PAPER.value:
                try:
                    level = AutonomyLevel(str(state.get("autonomy", "")))
                except ValueError:
                    self.last_source = "invalid level in heartbeat"
                    return FAIL_SAFE
                self.last_source = f"running bot (heartbeat {age:.0f}s old)"
                return level
        self.last_source = "bot not running: persisted level"
        return persisted_autonomy(self.repos, self.cfg)


__all__ = ["FAIL_SAFE", "PublishedAutonomy", "last_dashboard_level", "persisted_autonomy"]
