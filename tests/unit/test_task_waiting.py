"""``core.tasks.wait_for_tasks``: waiting on a live set of background tasks
must never spin the event loop (Python 3.12+ ``gather`` over finished tasks
does not yield), never wait for the calling task, and must pick up tasks
added while it waits."""

from __future__ import annotations

import asyncio

from core.tasks import wait_for_tasks


async def test_returns_when_the_set_still_holds_finished_tasks() -> None:
    async def noop() -> None:
        return None

    t = asyncio.get_running_loop().create_task(noop())
    tasks = {t}
    await asyncio.sleep(0)  # t finishes; no done-callback removes it from the set
    assert t.done() and tasks
    await asyncio.wait_for(wait_for_tasks(tasks), timeout=1.0)  # the old loop spun here forever


async def test_waits_for_tasks_added_while_waiting() -> None:
    tasks: set[asyncio.Task[None]] = set()
    done: list[str] = []
    loop = asyncio.get_running_loop()

    async def second() -> None:
        await asyncio.sleep(0.01)
        done.append("second")

    async def first() -> None:
        await asyncio.sleep(0.01)
        tasks.add(loop.create_task(second()))
        done.append("first")

    tasks.add(loop.create_task(first()))
    await asyncio.wait_for(wait_for_tasks(tasks), timeout=1.0)
    assert done == ["first", "second"]


async def test_a_task_never_waits_for_itself() -> None:
    tasks: set[asyncio.Task[None]] = set()

    async def me() -> None:
        await wait_for_tasks(tasks)  # I am in the set

    t = asyncio.get_running_loop().create_task(me())
    tasks.add(t)
    await asyncio.wait_for(t, timeout=1.0)


async def test_a_kill_triggered_from_a_position_task_does_not_freeze_the_loop() -> None:
    """The original hang: a resting stop that cannot be cancelled after a close
    escalates from inside a position-manager task; the kill handler then waits
    for the position manager to go idle."""
    from tests.unit.test_resting_sell_invariant import test_a_stop_left_on_a_closed_trade_escalates

    await asyncio.wait_for(test_a_stop_left_on_a_closed_trade_escalates(), timeout=20)
