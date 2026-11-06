"""In-process publish/subscribe event bus.

Handlers may be sync or async. A failing handler is logged and isolated — it
never breaks publication to other handlers or the publisher. Recent events are
kept per topic for the dashboard.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.clock import now_ist
from core.tasks import wait_for_tasks

log = logging.getLogger(__name__)


class Topic(StrEnum):
    CANDLE = "candle"
    SIGNAL = "signal"
    SIGNAL_FILTERED = "signal_filtered"
    ENTRY_DECISION = "entry_decision"
    ENGINE_STATE = "engine_state"
    MODE_CHANGE = "mode_change"
    STRATEGY_ERROR = "strategy_error"
    DAY_RESET = "day_reset"


@dataclass(frozen=True)
class Event:
    topic: Topic
    payload: Any
    at: datetime = field(default_factory=now_ist)


Handler = Callable[[Event], Any]


class EventBus:
    def __init__(self, history: int = 200, *, now_fn: Callable[[], datetime] = now_ist) -> None:
        self._handlers: dict[Topic, list[Handler]] = defaultdict(list)
        self._recent: dict[Topic, deque[Event]] = defaultdict(lambda: deque(maxlen=history))
        self._tasks: set[asyncio.Task[Any]] = set()
        self._now = now_fn  # the bot's clock: simulated time in backtests
        self.errors = 0

    def subscribe(self, topic: Topic, handler: Handler) -> Callable[[], None]:
        self._handlers[topic].append(handler)
        return lambda: self._handlers[topic].remove(handler)

    def publish(self, topic: Topic, payload: Any) -> Event:
        ev = Event(topic, payload, self._now())
        self._recent[topic].append(ev)
        for h in list(self._handlers[topic]):
            try:
                result = h(ev)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    self._tasks.add(task)
                    task.add_done_callback(self._done)
            except Exception:
                self.errors += 1
                log.exception("event handler failed", extra={"topic": topic.value})
        return ev

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.errors += 1
            log.error("async event handler failed", exc_info=task.exception())

    def recent(self, topic: Topic, n: int = 50) -> list[Event]:
        return list(self._recent[topic])[-n:]

    async def drain(self) -> None:
        await wait_for_tasks(self._tasks)
