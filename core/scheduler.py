"""Session scheduler (APScheduler, Asia/Kolkata).

Registers the bot's recurring jobs from plain callables (sync or async), so
``core`` stays free of project imports. All jobs coalesce and never overlap
(``max_instances=1``), so a slow run cannot pile up.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.clock import IST

log = logging.getLogger(__name__)

Job = Callable[[], Any]


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    func: Job
    every_seconds: float | None = None
    at: time | None = None  # daily, Mon-Fri


def _wrap(job_id: str, func: Job) -> Callable[[], Any]:
    async def runner() -> None:
        try:
            result = func()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("scheduled job failed", extra={"job": job_id})

    return runner


class TradingScheduler:
    def __init__(self, scheduler: AsyncIOScheduler | None = None) -> None:
        self._scheduler = scheduler or AsyncIOScheduler(timezone=IST)
        self.jobs: dict[str, JobSpec] = {}

    def every(self, job_id: str, seconds: float, func: Job) -> None:
        self._add(JobSpec(job_id, func, every_seconds=seconds))

    def daily(self, job_id: str, at: time, func: Job) -> None:
        self._add(JobSpec(job_id, func, at=at))

    def _add(self, spec: JobSpec) -> None:
        if spec.job_id in self.jobs:
            raise ValueError(f"duplicate job id {spec.job_id}")
        trigger: Any
        if spec.every_seconds is not None:
            trigger = IntervalTrigger(seconds=spec.every_seconds, timezone=IST)
        else:
            assert spec.at is not None
            trigger = CronTrigger(day_of_week="mon-fri", hour=spec.at.hour, minute=spec.at.minute, timezone=IST)
        self._scheduler.add_job(_wrap(spec.job_id, spec.func), trigger, id=spec.job_id, max_instances=1,
                                coalesce=True, misfire_grace_time=30)
        self.jobs[spec.job_id] = spec

    async def run_now(self, job_id: str) -> None:
        """Execute a job immediately (tests, manual triggers)."""
        await _wrap(job_id, self.jobs[job_id].func)()

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)
