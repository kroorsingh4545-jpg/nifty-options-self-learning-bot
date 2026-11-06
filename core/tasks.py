"""Waiting for a set of background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


async def wait_for_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Return once every task in ``tasks`` is done, including tasks added
    while waiting (pass the live set, not a copy).

    Only UNFINISHED tasks are awaited. Since Python 3.12, ``asyncio.gather``
    over tasks that are all finished completes without yielding to the event
    loop, and a finished task leaves its set only through a done-callback that
    needs the loop to run. ``while tasks: await gather(*tasks)`` therefore
    spins forever, freezing the whole event loop (kill switch, square-off,
    heartbeat), whenever a finished task has not been removed yet.

    The calling task is never awaited (a task waiting for itself deadlocks).
    """
    me = asyncio.current_task()
    while True:
        pending = [t for t in list(tasks) if not t.done() and t is not me]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["wait_for_tasks"]
