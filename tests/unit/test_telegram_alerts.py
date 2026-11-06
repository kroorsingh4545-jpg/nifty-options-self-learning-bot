"""Telegram alerts (Phase 10): what is sent, that sending can never block or
break trading, rate limiting, secrets, and the entry / exit / error / daily
loss / kill switch / end-of-day messages from a running PAPER session.

No test talks to Telegram: an ``httpx.MockTransport`` stands in for it.
Market data is SYNTHETIC (tests only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from config.settings import TelegramSection
from core.enums import RiskSeverity, TradingMode
from core.masking import DEFAULT_MASKER
from monitoring.alerts import Alert, AlertKind, AlertLogHandler, MemoryAlertSink
from monitoring.telegram import TelegramAlertSink, format_message
from risk.kill_switch import KillReason
from runtime.notifications import end_of_day_alert
from tests.fakes import FakeWallClock
from tests.paper_support import SID, FakeConnections, drive, paper_cfg, session
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY

TOKEN = "123456789:AAH-fake-telegram-token-for-tests-only"


class Telegram:
    """A fake Telegram Bot API (MockTransport handler)."""

    def __init__(self, statuses: list[int] | None = None, retry_after: int = 1) -> None:
        self.statuses = list(statuses or [])
        self.retry_after = retry_after
        self.messages: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            self.messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if status == 429:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": self.retry_after}})
        return httpx.Response(status, json={"ok": False})


def sink(api: Telegram, settings: TelegramSection | None = None, sleeps: list[float] | None = None
         ) -> TelegramAlertSink:
    async def fake_sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)
        await asyncio.sleep(0)

    return TelegramAlertSink(token=TOKEN, chat_id="42", settings=settings or TelegramSection(),
                             http=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=fake_sleep)


def alert(kind: AlertKind = AlertKind.GENERAL, sev: RiskSeverity = RiskSeverity.INFO, title: str = "t") -> Alert:
    return Alert(sev, title, "message", strategy_id=SID, kind=kind)


async def flush(s: TelegramAlertSink) -> None:
    await s.aclose(timeout=5)


async def test_entries_exits_errors_eod_and_criticals_are_sent() -> None:
    api = Telegram()
    s = sink(api)
    await s.start()
    for k in (AlertKind.ENTRY, AlertKind.EXIT, AlertKind.ERROR, AlertKind.EOD_SUMMARY):
        s.send(alert(k))
    s.send(alert(AlertKind.KILL_SWITCH, RiskSeverity.CRITICAL, "Kill switch triggered (GLOBAL)"))
    s.send(alert(AlertKind.GENERAL, RiskSeverity.INFO, "chatty"))  # below min_severity: not sent
    s.send(alert(AlertKind.GENERAL, RiskSeverity.WARNING, "Strategy paused"))
    await flush(s)
    texts = [m["text"] for m in api.messages]
    assert len(texts) == 6 and not any("chatty" in t for t in texts)
    assert all(m["chat_id"] == "42" and "[PAPER]" in m["text"] for m in api.messages)
    assert any("KILL SWITCH" in t and "🚨" in t for t in texts)


async def test_kinds_can_be_switched_off_but_criticals_cannot() -> None:
    api = Telegram()
    s = sink(api, TelegramSection(entries=False, exits=False, errors=False, eod_summary=False))
    await s.start()
    s.send(alert(AlertKind.ENTRY))
    s.send(alert(AlertKind.EXIT, RiskSeverity.CRITICAL))
    s.send(alert(AlertKind.DAILY_LOSS, RiskSeverity.CRITICAL))
    await flush(s)
    assert len(api.messages) == 2


async def test_rate_limit_429_waits_and_retries() -> None:
    api = Telegram([429, 200], retry_after=7)
    sleeps: list[float] = []
    s = sink(api, sleeps=sleeps)
    await s.start()
    s.send(alert(AlertKind.ENTRY))
    s.send(alert(AlertKind.EXIT))
    await flush(s)
    assert len(api.messages) == 2 and 7.0 in sleeps
    assert sleeps.count(TelegramSection().min_interval_seconds) >= 2  # paced between messages


async def test_a_rejected_token_disables_the_channel() -> None:
    api = Telegram([401])
    s = sink(api)
    await s.start()
    s.send(alert(AlertKind.ENTRY))
    await asyncio.sleep(0.05)
    s.send(alert(AlertKind.EXIT))
    await flush(s)
    assert s.disabled == "HTTP 401" and not api.messages and "disabled" in s.describe()


async def test_send_never_blocks_or_raises_when_telegram_is_down() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    s = TelegramAlertSink(token=TOKEN, chat_id="42", settings=TelegramSection(max_attempts=2, queue_size=10),
                          http=httpx.AsyncClient(transport=httpx.MockTransport(down)),
                          sleep=lambda _s: asyncio.sleep(0))
    await s.start()
    for _ in range(25):
        s.send(alert(AlertKind.ENTRY))  # returns at once; the queue is bounded
    s.send(alert(AlertKind.KILL_SWITCH, RiskSeverity.CRITICAL))  # a critical always gets a slot
    assert s.dropped >= 15 and s._queue[-1].kind is AlertKind.KILL_SWITCH
    await flush(s)
    assert s.failed >= 1 and s.sent == 0


async def test_send_is_thread_safe() -> None:
    api = Telegram()
    s = sink(api)
    await s.start()
    threads = [threading.Thread(target=s.send, args=(alert(AlertKind.ERROR),)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    await asyncio.sleep(0.05)
    await flush(s)
    assert len(api.messages) == 5


def test_messages_mask_secrets_and_fit_telegram() -> None:
    DEFAULT_MASKER.register_many(["upstox-secret-value-123"])
    a = Alert(RiskSeverity.CRITICAL, "Auth", "token upstox-secret-value-123 rejected " + "x" * 5000,
              kind=AlertKind.ERROR)
    text = format_message(a, TradingMode.PAPER)
    assert "upstox-secret-value-123" not in text and len(text) <= 4000


async def test_the_bot_token_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    from monitoring.json_logger import JsonFormatter

    api = Telegram([400])
    s = sink(api)
    await s.start()
    with caplog.at_level(logging.DEBUG):
        s.send(alert(AlertKind.ENTRY))
        await flush(s)
    formatter = JsonFormatter(DEFAULT_MASKER)
    rendered = "\n".join(formatter.format(r) for r in caplog.records)
    assert TOKEN not in rendered and TOKEN.split(":")[1] not in rendered
    assert TOKEN in api.urls[0]  # only in the request URL


def test_error_logs_become_deduplicated_error_alerts() -> None:
    mem = MemoryAlertSink()
    handler = AlertLogHandler(mem, dedupe_seconds=300)
    lg = logging.getLogger("execution.test_component")
    lg.addHandler(handler)
    try:
        lg.error("protective stop failed")
        lg.error("protective stop failed")  # duplicate inside the window
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            lg.exception("job failed")
        lg.warning("only a warning")
        logging.getLogger("alerts").error("ALERT: echo")  # the alert log itself never loops back
        logging.getLogger("monitoring.telegram").error("telegram down")  # nor the channel's own errors
    finally:
        lg.removeHandler(handler)
    titles = [a.title for a in mem.alerts]
    assert titles == ["protective stop failed", "job failed"]
    assert all(a.kind is AlertKind.ERROR for a in mem.alerts)
    assert "RuntimeError" in mem.alerts[1].message


# --------------------------------------------------------------------------- #
# A PAPER session sends the right alerts
# --------------------------------------------------------------------------- #


async def test_session_alerts_entry_exit_kill_and_end_of_day(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    captured = MemoryAlertSink()
    s = session(cfg, wall, conns)
    s._alert_sinks = (captured,)
    bot = await s.start()
    try:
        await drive(s, conns, ORB_DAY)
        kinds = [a.kind for a in captured.alerts]
        assert AlertKind.ENTRY in kinds
        entry = next(a for a in captured.alerts if a.kind is AlertKind.ENTRY)
        assert entry.strategy_id == SID and "BUY" in entry.message
        bot.stack.kill_switch.trigger(KillReason.MANUAL, "operator test")
        await bot.settle()
        assert any(a.kind is AlertKind.KILL_SWITCH and a.severity is RiskSeverity.CRITICAL for a in captured.alerts)
        assert any(a.kind is AlertKind.EXIT for a in captured.alerts)  # the kill switch exited the trade
        exit_alert = next(a for a in captured.alerts if a.kind is AlertKind.EXIT)
        assert "KILL_SWITCH" in exit_alert.message and "net Rs" in exit_alert.message
        eod = end_of_day_alert(bot)
        assert eod is not None and eod.kind is AlertKind.EOD_SUMMARY
        assert f"{SID}: 1 trades" in eod.message and "Kill switch: active" in eod.message
    finally:
        await s.shutdown()


async def test_daily_loss_breach_is_its_own_alert_kind(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    captured = MemoryAlertSink()
    s = session(cfg, wall, FakeConnections(wall))
    s._alert_sinks = (captured,)
    bot = await s.start()
    try:
        bot.stack.kill_switch.trigger(KillReason.STRATEGY_DAILY_LOSS, "daily loss 5000 reached limit",
                                      strategy_id=SID)
        await bot.settle()
        a = next(x for x in captured.alerts if x.kind is AlertKind.DAILY_LOSS)
        assert a.severity is RiskSeverity.CRITICAL and a.strategy_id == SID
    finally:
        await s.shutdown()


def test_no_eod_summary_on_a_holiday(tmp_path: Path) -> None:
    from datetime import date

    class Bot:  # only what end_of_day_alert reads before deciding
        cfg = paper_cfg(tmp_path)

        @staticmethod
        def _now() -> Any:
            return at(15, 35)

    assert end_of_day_alert(Bot(), date(2026, 11, 10)) is None  # type: ignore[arg-type]  # Diwali holiday
