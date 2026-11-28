"""CRITICAL and kill-switch Telegram alerts retry until delivered (owner-approved A, 2026-11-28).

On 25 Nov 11:34 and 27 Nov 13:01 the office internet went down for about 1 and 11 minutes; both kill-switch
alerts were given up after 3 attempts (~10 s) and never arrived. Now a CRITICAL alert waits in the queue and
is retried (back-off capped at ``critical_retry_max_seconds``) until Telegram takes it, in order, with a
"delayed" note; other alerts keep their ``max_attempts``. Sending still never blocks trading.

No test talks to Telegram: an ``httpx.MockTransport`` stands in for it. Market data is SYNTHETIC.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from config.settings import TelegramSection
from core.clock import IST
from core.enums import RiskSeverity
from monitoring.alerts import Alert, AlertKind
from monitoring.telegram import CRITICAL_QUEUE_FACTOR, TelegramAlertSink
from risk.kill_switch import KillReason
from tests.fakes import FakeWallClock
from tests.paper_support import FakeConnections, drive, paper_cfg, session
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY

TOKEN = "123456789:AAH-fake-telegram-token-for-tests-only"
T0 = datetime(2026, 11, 27, 13, 1, 29, tzinfo=IST)


class Outage:
    """Fake Telegram: the first ``down`` requests fail (``error`` or an HTTP ``status``), then it works."""

    def __init__(self, down: int, error: type[Exception] | None = httpx.ConnectError, status: int | None = None,
                 clock: FakeWallClock | None = None, seconds_per_failure: float = 0.0) -> None:
        self.down = down
        self.error = error
        self.status = status
        self.calls = 0
        self.messages: list[str] = []
        self.clock = clock
        self.seconds_per_failure = seconds_per_failure

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.down:
            if self.clock is not None:
                self.clock.advance(seconds=self.seconds_per_failure)
            if self.status is not None:
                return httpx.Response(self.status, json={"ok": False, "parameters": {"retry_after": 2}})
            assert self.error is not None
            raise self.error("internet down")
        self.messages.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


def make_sink(api: Any, settings: TelegramSection | None = None, sleeps: list[float] | None = None,
              now: Any = None) -> TelegramAlertSink:
    async def fake_sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)
        await asyncio.sleep(0)

    kw: dict[str, Any] = {"now_fn": now} if now is not None else {}
    return TelegramAlertSink(token=TOKEN, chat_id="42", settings=settings or TelegramSection(),
                             http=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=fake_sleep, **kw)


def kill(at_: datetime | None = None, title: str = "Kill switch triggered (GLOBAL)") -> Alert:
    a = Alert(RiskSeverity.CRITICAL, title,
              "WEBSOCKET_DISCONNECT: market_data websocket down for more than 30s - active until a named human "
              "resets it", kind=AlertKind.KILL_SWITCH)
    return a if at_ is None else Alert(a.severity, a.title, a.message, kind=a.kind, at=at_)


def info(kind: AlertKind = AlertKind.ENTRY, title: str = "Entry") -> Alert:
    return Alert(RiskSeverity.INFO, title, "BUY 65 NIFTY 25750 PE", kind=kind)


# --------------------------------------------------------------------------- #
async def test_a_kill_alert_survives_an_eleven_minute_outage_and_arrives_in_order() -> None:
    api = Outage(down=40)  # 40 failed attempts: far beyond max_attempts (3)
    sleeps: list[float] = []
    s = make_sink(api, sleeps=sleeps)
    await s.start()
    s.send(info(AlertKind.ENTRY, "Entry before"))  # non-critical: given up after max_attempts
    s.send(kill())
    s.send(info(AlertKind.EXIT, "Exit after"))
    await s.aclose(timeout=5)
    assert len(api.messages) == 2
    assert "KILL SWITCH" in api.messages[0] and "WEBSOCKET_DISCONNECT" in api.messages[0]
    assert "Exit after" in api.messages[1]  # queued behind the kill alert, delivered after it
    assert s.failed == 1 and s.delivered_late == 1 and s.sent == 2
    retry = [x for x in sleeps if x != TelegramSection().min_interval_seconds]
    assert max(retry) == TelegramSection().critical_retry_max_seconds  # back-off capped at 30 s
    # 11 minutes of 30-s waits: the alert arrives within 30 s of the internet coming back
    assert sum(retry) >= 11 * 60 - 60


@pytest.mark.parametrize(("error", "status"), [(httpx.ConnectTimeout, None), (httpx.ReadTimeout, None),
                                               (httpx.ConnectError, None), (None, 502), (None, 503), (None, 429)])
async def test_criticals_retry_through_every_temporary_failure(error: Any, status: int | None) -> None:
    api = Outage(down=8, error=error, status=status)
    s = make_sink(api)
    await s.start()
    s.send(kill())
    await s.aclose(timeout=5)
    assert len(api.messages) == 1 and api.calls == 9 and s.failed == 0


async def test_non_critical_alerts_still_give_up_after_max_attempts() -> None:
    api = Outage(down=10)
    s = make_sink(api, TelegramSection(max_attempts=3))
    await s.start()
    s.send(info())
    await s.aclose(timeout=5)
    assert api.calls == 3 and s.failed == 1 and not api.messages


async def test_a_rejected_token_or_message_is_never_retried_forever(caplog: pytest.LogCaptureFixture) -> None:
    api = Outage(down=1000, status=401)
    s = make_sink(api)
    await s.start()
    s.send(kill())
    await s.aclose(timeout=5)
    assert api.calls == 1 and s.disabled == "HTTP 401"

    bad = Outage(down=1000, status=400)
    s2 = make_sink(bad)
    await s2.start()
    s2.send(kill())
    await s2.aclose(timeout=5)
    assert bad.calls == 1 and s2.failed == 1  # Telegram refused this text: the same text would fail again


async def test_a_late_critical_says_when_it_really_happened() -> None:
    clock = FakeWallClock(T0)
    api = Outage(down=22, clock=clock, seconds_per_failure=30.0)  # 11 minutes down
    s = make_sink(api, now=clock)
    await s.start()
    s.send(kill(at_=T0))
    await s.aclose(timeout=5)
    (text,) = api.messages
    assert text.startswith("⏳ DELAYED: Telegram was unreachable; this happened 11 min 00 s ago at 13:01:29 IST.")
    assert "WEBSOCKET_DISCONNECT" in text and len(text) <= 4000

    ontime = Outage(down=0)
    s2 = make_sink(ontime, now=FakeWallClock(T0))
    await s2.start()
    s2.send(kill(at_=T0))
    await s2.aclose(timeout=5)
    assert not ontime.messages[0].startswith("⏳")


def test_a_full_queue_never_drops_a_critical() -> None:
    s = TelegramAlertSink(token=TOKEN, chat_id="42", settings=TelegramSection(queue_size=10))
    for i in range(10):
        s.send(info(title=f"entry {i}"))
    for i in range(3):
        s.send(kill(title=f"kill {i}"))
    titles = [a.title for a in s._queue]
    assert titles[-3:] == ["kill 0", "kill 1", "kill 2"] and len(titles) == 10
    assert titles[:7] == [f"entry {i}" for i in range(3, 10)]  # the OLDEST non-critical ones made room
    for i in range(3, 10):
        s.send(kill(title=f"kill {i}"))
    assert all(a.severity is RiskSeverity.CRITICAL for a in s._queue) and len(s._queue) == 10
    s.send(kill(title="kill 10"))  # all critical: the queue grows past queue_size instead of dropping one
    s.send(info(title="late entry"))  # a non-critical is refused while the queue is full
    assert len(s._queue) == 11 and s.critical_dropped == 0
    assert s._queue[-1].title == "kill 10"


def test_the_hard_cap_is_the_only_way_a_critical_is_dropped() -> None:
    s = TelegramAlertSink(token=TOKEN, chat_id="42", settings=TelegramSection(queue_size=10))
    cap = CRITICAL_QUEUE_FACTOR * 10
    for i in range(cap + 2):
        s.send(kill(title=f"kill {i}"))
    assert len(s._queue) == cap and s.critical_dropped == 2 and s._queue[-1].title == f"kill {cap + 1}"


async def test_shutdown_during_an_outage_logs_the_undelivered_criticals(caplog: pytest.LogCaptureFixture) -> None:
    api = Outage(down=10**6)
    s = make_sink(api)
    await s.start()
    s.send(kill(title="Kill switch triggered (GLOBAL)"))
    s.send(kill(title="Kill switch action FAILED"))
    await asyncio.sleep(0.01)
    assert "critical waiting for Telegram" in s.describe()
    with caplog.at_level(logging.ERROR):
        await s.aclose(timeout=0.05)
    rec = [r for r in caplog.records if "CRITICAL alerts NOT delivered" in r.getMessage()]
    assert rec and getattr(rec[0], "count", None) == 2
    assert getattr(rec[0], "titles", None) == ["Kill switch triggered (GLOBAL)", "Kill switch action FAILED"]
    assert TOKEN not in caplog.text


async def test_retrying_never_blocks_the_caller_or_the_event_loop() -> None:
    api = Outage(down=10**6)
    s = make_sink(api)
    await s.start()
    s.send(kill())
    loop_turns = 0
    for _ in range(200):
        started = asyncio.get_running_loop().time()
        s.send(info())  # returns at once while the worker keeps retrying
        assert asyncio.get_running_loop().time() - started < 0.05
        await asyncio.sleep(0)
        loop_turns += 1
    assert loop_turns == 200 and api.calls > 1
    await s.aclose(timeout=0.05)


async def test_a_paper_session_kill_during_an_outage_is_delivered_after_it(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    api = Outage(down=0)
    sink = make_sink(api)
    s = session(cfg, wall, conns)
    s._alert_sinks = (sink,)
    bot = await s.start()
    try:
        await drive(s, conns, ORB_DAY)
        api.down = api.calls + 25  # the internet goes down now, for the next 25 requests
        bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "market_data websocket down for more than 30s")
        await bot.settle()
        assert bot.stack.positions.open_trade_count() == 0  # the kill's exit ran while Telegram was down
        for _ in range(400):
            if any("KILL SWITCH" in m for m in api.messages):
                break
            await asyncio.sleep(0)
        kill_msgs = [m for m in api.messages if "KILL SWITCH" in m]
        assert kill_msgs and "WEBSOCKET_DISCONNECT" in kill_msgs[0]
        assert sink.delivered_late >= 1
    finally:
        await s.shutdown()


def test_the_retry_cap_is_configurable_and_bounded() -> None:
    assert TelegramSection().critical_retry_max_seconds == 30.0
    with pytest.raises(ValueError):
        TelegramSection(critical_retry_max_seconds=1.0)
    with pytest.raises(ValueError):
        TelegramSection(critical_retry_max_seconds=3600.0)
    s = TelegramAlertSink(token=TOKEN, chat_id="42", settings=TelegramSection(critical_retry_max_seconds=10.0))
    assert [s._backoff(n, True) for n in (1, 2, 3, 4, 5, 9)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert s._backoff(9, False) == 30.0
    assert timedelta(seconds=s._backoff(100, True)) <= timedelta(seconds=10)
