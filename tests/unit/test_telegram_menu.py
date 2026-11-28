"""Telegram control menu (weekend-1 task 3, owner choice "A").

No test talks to Telegram: an ``httpx.MockTransport`` stands in for the Bot
API. Market data is SYNTHETIC (tests only).

Covers: other chats ignored; every read-only button; every control needs a
confirmation; forged / expired / reused button data; reset refused while the
feed is down (also re-checked when the bot runs it); reset accepted on a
stable feed and strategies re-activated; the WEBSOCKET_DISCONNECT recovery
message after 2 minutes stable (never an automatic reset); Telegram down does
not affect trading; dashboard + Telegram at once; rate limiting; the launcher
wiring; and the architecture guard (no LIVE / risk / config path).
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from config.settings import TelegramMenuSection
from core.enums import ControlCommandStatus, ControlCommandType, OrderSide, StrategyStatus, TradeStatus
from dashboard.commands import CommandClient
from execution.operator_controls import REACTIVATED_EVENT
from monitoring.telegram import TelegramBotApi, parse_update
from risk.kill_switch import KillReason
from runtime.paper import default_menu_api
from runtime.telegram_menu import ALLOWED_COMMANDS, BUTTON_ID, CONTROLS, VIEWS, TelegramMenu, menu_buttons
from tests.synthetic import at
from tests.unit.test_bot_integration import Sim
from tests.unit.test_control_plane import WALL, Wall, enqueue, run, traded_sim
from tests.unit.test_day_rollover_and_recovery import statuses

TOKEN = "123456789:AAH-fake-telegram-token-for-tests-only"
CHAT = "424242"
OTHER_CHAT = "999999"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ids = itertools.count(1000)


class FakeTelegram:
    """Fake Bot API: queued updates for getUpdates; records sends and answers."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.methods: list[str] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("telegram unreachable")
        method = request.url.path.rsplit("/", 1)[-1]
        self.methods.append(method)
        body = json.loads(request.content or b"{}")
        if method == "getUpdates":
            offset = body.get("offset") or 0
            out = [u for u in self.updates if u["update_id"] >= offset]
            self.updates = []
            return httpx.Response(200, json={"ok": True, "result": out})
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "answerCallbackQuery":
            self.answers.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False})

    @property
    def last(self) -> dict[str, Any]:
        assert self.sent, "no message was sent"
        return self.sent[-1]

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


def buttons(message: dict[str, Any]) -> list[str]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b["callback_data"] for row in rows for b in row]


def text_update(text: str, chat: str = CHAT, user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or {"id": int(CHAT), "username": "desk_owner", "first_name": "Desk"}
    return {"update_id": next(_ids), "message": {"message_id": 1, "chat": {"id": int(chat), "type": "private"},
                                                  "from": user, "text": text, "date": 0}}


def press_update(data: str, chat: str = CHAT, user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or {"id": int(CHAT), "username": "desk_owner", "first_name": "Desk"}
    return {"update_id": next(_ids), "callback_query": {
        "id": f"cb{next(_ids)}", "from": user, "data": data,
        "message": {"message_id": 7, "chat": {"id": int(chat), "type": "private"}}}}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.001))


class Rig:
    def __init__(self, sim: Sim, tmp_path: Path, settings: TelegramMenuSection | None = None) -> None:
        self.sim = sim
        self.tg = FakeTelegram()
        self.wall: Wall = sim.bot.control._wall  # type: ignore[assignment]
        self.clock = Clock()
        self.conn: dict[str, Any] = {"websocket_connected": True, "websocket_stale": False,
                                     "websocket_state": "CONNECTED", "reconnects": 0, "upstox_connected": True}
        sim.bot.control.set_status_providers(connection_status=lambda: dict(self.conn))
        self.api = TelegramBotApi(token=TOKEN, chat_id=CHAT,
                                  http=httpx.AsyncClient(transport=httpx.MockTransport(self.tg)))
        self.log_path = tmp_path / "bot.jsonl"
        self.research = tmp_path / "research"
        self.menu = TelegramMenu(sim.bot, self.api, settings or TelegramMenuSection(), log_path=self.log_path,
                                 research_dir=self.research, wall_fn=self.wall, clock=self.clock, sleep=fast_sleep)

    async def publish(self, advance: float = 0.0) -> None:
        self.wall.now = self.wall.now + timedelta(seconds=advance)
        await self.sim.bot.control.publish_state()

    async def stable_feed(self, seconds: float = 121.0) -> None:
        await self.publish()
        step = 5.0
        held = 0.0
        while held < seconds:
            await self.publish(step)
            held += step

    async def press(self, data: str, **kw: Any) -> None:
        self.clock.t += 2.0  # outside the rate limit
        await self.menu.handle(press_update(data, **kw))

    async def say(self, text: str, **kw: Any) -> None:
        self.clock.t += 2.0
        await self.menu.handle(text_update(text, **kw))

    async def confirm_last(self) -> None:
        yes = [b for b in buttons(self.tg.last) if b.startswith("y:")]
        assert len(yes) == 1, self.tg.last
        await self.press(yes[0])

    async def run_queue(self) -> None:
        await self.sim.bot.control.process_pending()
        await self.sim.bot.control.wait_idle()
        await self.sim.bot.settle()
        await self.menu.wait_idle()


def telegram_commands(sim: Sim) -> list[Any]:
    return [c for c in sim.repos.commands.recent(100) if c.payload_json.get("source") == "telegram"]


@pytest.fixture
async def rig(tmp_path: Path) -> Rig:
    sim = await traded_sim()
    r = Rig(sim, tmp_path)
    await r.publish()
    return r


# --------------------------------------------------------------------------- #
# Security: only the configured chat
# --------------------------------------------------------------------------- #
async def test_unauthorised_chats_and_users_are_ignored_silently_and_logged(
        rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    stranger = {"id": 5555, "username": "stranger"}
    with caplog.at_level(logging.WARNING):
        await rig.say("menu", chat=OTHER_CHAT, user=stranger)
        await rig.press("c:kill", chat=OTHER_CHAT, user=stranger)
        await rig.press("v:status", chat=OTHER_CHAT, user=stranger)
    assert rig.tg.sent == [] and rig.tg.answers == []  # not even the button spinner is answered
    assert rig.menu.ignored == 3
    assert telegram_commands(rig.sim) == [] and rig.sim.repos.commands.pending() == []
    ignored = [r for r in caplog.records if "telegram update ignored" in r.getMessage()]
    assert len(ignored) == 3 and all(getattr(r, "chat_id", None) == OTHER_CHAT for r in ignored)


async def test_menu_word_shows_every_button_and_nothing_else(rig: Rig) -> None:
    for word in ("Menu", "/menu", "/start", "/menu@NiftyBot"):
        await rig.say(word)
        assert buttons(rig.tg.last) == [b for row in menu_buttons() for _, b in row]
    assert set(buttons(rig.tg.last)) == {f"v:{v}" for v in VIEWS} | {f"c:{c}" for c in CONTROLS}
    await rig.say("hello")
    assert "Send Menu" in rig.tg.last["text"]


# --------------------------------------------------------------------------- #
# Read-only buttons
# --------------------------------------------------------------------------- #
def _write_reports(research: Path) -> None:
    research.mkdir(parents=True, exist_ok=True)
    (research / "variants-2026-11-02.txt").write_text("old\n", encoding="utf-8")
    (research / "variants-2026-11-03.txt").write_text(
        "Forward shadow variants sv.v1 next to the current ORB (...)\n"
        "  session               ORB now    A: their exit          A - ORB    B: 30m retest\n"
        "  2026-11-02         1 | +5,303         1 | -481       1 | -5,784           0 | +0\n"
        "  counted              4 | +377       4 | -1,835       4 | -2,212           0 | +0\n"
        "  A (A - current ORB per signal): not yet evaluable, 4 of 525 trades\n"
        "  B (B net per trade): not yet evaluable, 0 of 625 trades\n", encoding="utf-8")
    (research / "shadow-2026-11-03.txt").write_text(
        "...\nShadow decision engine d7.v1 (pre-registered, fingerprint 4632; never affects orders):\n"
        "signal time          dir  verdict                p*      conf   bias (source)    counted  R\n"
        "2026-11-02T10:20:00  CE   NO_TRADE               0.568   0.18   0.641 (STATE)    yes       1.31\n"
        "2026-11-02T11:00:00  CE   TRADE                  0.548   0.14   0.641 (STATE)    yes       0.66\n"
        "2026-10-30T11:00:00  PE   NO_TRADE   0.590   0.14   0.641 (STATE)    no (before registration)  0.6\n",
        encoding="utf-8")


def _write_log(path: Path) -> None:
    lines = [{"ts": f"2026-11-03T10:{m:02d}:00.000+05:30", "level": "WARNING", "event": f"warning number {m}"}
             for m in range(20)]
    lines.insert(3, {"ts": "2026-11-03T10:03:30.000+05:30", "level": "INFO", "event": "routine info line"})
    lines.append({"ts": "2026-11-03T10:30:00.000+05:30", "level": "ERROR", "event": f"bad thing {TOKEN}",
                  "strategy_id": "orb_options"})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")


@pytest.mark.parametrize(("view", "expected"), [
    ("status", ["NIFTY bot · PAPER", "Feed: connected", "Kill switch: not active", "Open positions: 1"]),
    ("positions", ["Open positions (1)", "(orb_options)", "stop"]),
    ("trades", ["No closed trades today", "Open positions: 1"]),
    ("health", ["Feed lag", "PC clock", "Reconnects: 0", "late ticks dropped"]),
    ("risk", ["orb_options: used", "vwap_ema: used", "GLOBAL: used", "left"]),
    ("log", ["warning number 19", "ERROR bad thing", "[orb_options]"]),
    ("signals", ["orb_options: CE at", "APPROVED", "vwap_ema: no signal"]),
    ("shadow", ["Shadow variants (2026-11-03)", "counted", "A (A - current ORB", "Decision engine (2026-11-03)",
                "counted signals 2 · TRADE 1 · NO_TRADE 1", "never affects orders"]),
])
async def test_each_read_only_button(rig: Rig, view: str, expected: list[str]) -> None:
    _write_log(rig.log_path)
    _write_reports(rig.research)
    await rig.press(f"v:{view}")
    text = rig.tg.last["text"]
    for part in expected:
        assert part in text, (part, text)
    assert buttons(rig.tg.last) == ["v:menu"]
    assert rig.sim.repos.commands.pending() == []  # read-only: nothing queued
    if view == "log":
        assert "routine info line" not in text and TOKEN not in text  # important lines only, secrets masked
        assert len(text.splitlines()) == 1 + 15
    if view == "shadow":
        assert "old" not in text  # the newest report only


async def test_read_only_views_before_state_and_without_reports(tmp_path: Path) -> None:
    sim = await traded_sim()
    r = Rig(sim, tmp_path)
    await r.press("v:status")
    assert "not published its state yet" in r.tg.last["text"]
    await r.press("v:shadow")
    assert "No research report yet" in r.tg.last["text"]
    await r.press("v:log")
    assert "No log file yet" in r.tg.last["text"]


async def test_todays_trades_lists_closed_trades(rig: Rig) -> None:
    (trade,) = rig.sim.bot.stack.positions.open_trades()
    await rig.sim.bot.stack.positions.exit_trade(trade.trade_uid, ExitReasonManual())
    await rig.sim.bot.settle()
    await rig.press("v:trades")
    assert "Today's closed trades" in rig.tg.last["text"] and "MANUAL" in rig.tg.last["text"]


def ExitReasonManual() -> Any:  # noqa: N802 - tiny helper
    from risk.stop_loss_manager import ExitReason
    return ExitReason.MANUAL


# --------------------------------------------------------------------------- #
# Controls: always "Are you sure?" first
# --------------------------------------------------------------------------- #
async def _open_control(rig: Rig, name: str) -> None:
    if name == "reset":
        rig.sim.bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")
        await rig.sim.bot.settle()
        await rig.stable_feed()
    await rig.press(f"c:{name}")
    if name == "exit":
        (choice,) = [b for b in buttons(rig.tg.last) if b.startswith("x:")]
        assert rig.sim.repos.commands.pending() == []
        await rig.press(choice)


@pytest.mark.parametrize("name", sorted(CONTROLS))
async def test_each_control_button_needs_a_confirmation(rig: Rig, name: str) -> None:
    await _open_control(rig, name)
    assert "Are you sure?" in rig.tg.last["text"]
    assert [b[:2] for b in buttons(rig.tg.last)] == ["y:", "n:"]
    assert rig.sim.repos.commands.pending() == []  # nothing queued before Yes

    no = [b for b in buttons(rig.tg.last) if b.startswith("n:")][0]
    yes = [b for b in buttons(rig.tg.last) if b.startswith("y:")][0]
    await rig.press(no)
    assert "Cancelled" in rig.tg.last["text"] and rig.sim.repos.commands.pending() == []
    await rig.press(yes)  # the No used up this confirmation
    assert "expired or was already used" in rig.tg.last["text"] and rig.sim.repos.commands.pending() == []

    await _open_control(rig, name)
    await rig.confirm_last()
    (cmd,) = rig.sim.repos.commands.pending()
    expected = {"pause": ControlCommandType.ENGINE_PAUSE, "resume": ControlCommandType.ENGINE_START,
                "reset": ControlCommandType.RESET_KILL_SWITCH, "kill": ControlCommandType.KILL_SWITCH,
                "exit": ControlCommandType.EXIT_POSITION}[name]
    assert cmd.command is expected
    assert cmd.operator == "@desk_owner" and cmd.payload_json == {"source": "telegram"}
    assert "Telegram" in cmd.note
    assert (cmd.expires_at is None) == (name == "kill")  # an emergency stop never expires
    await rig.run_queue()
    assert rig.tg.last["text"].startswith("✅"), rig.tg.last["text"]


async def test_pause_resume_kill_and_exit_run_through_the_control_queue(rig: Rig) -> None:
    sim = rig.sim
    await rig.press("c:pause")
    await rig.confirm_last()
    await rig.run_queue()
    assert sim.bot.engine is not None and not sim.bot.engine.accepting_signals
    await rig.press("c:resume")
    await rig.confirm_last()
    await rig.run_queue()
    assert sim.bot.engine.accepting_signals

    (trade,) = sim.bot.stack.positions.open_trades()
    await rig.press("c:exit")
    await rig.press([b for b in buttons(rig.tg.last) if b.startswith("x:")][0])
    await rig.confirm_last()
    await rig.run_queue()
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "MANUAL"
    exits = [e for e in sim.repos.risk_events.recent(50) if e.event_type == "OPERATOR_EXIT"]
    assert exits and exits[0].details_json["operator"] == "@desk_owner"

    await rig.press("c:kill")
    await rig.confirm_last()
    await rig.run_queue()
    ev = sim.bot.stack.kill_switch.global_event
    assert ev is not None and ev.reason is KillReason.MANUAL and ev.message.startswith("telegram (@desk_owner)")
    done = [c for c in telegram_commands(sim) if c.status is ControlCommandStatus.DONE]
    assert {c.command for c in done} == {ControlCommandType.ENGINE_PAUSE, ControlCommandType.ENGINE_START,
                                         ControlCommandType.EXIT_POSITION, ControlCommandType.KILL_SWITCH}


async def test_operator_name_setting_overrides_the_account_name(tmp_path: Path) -> None:
    sim = await traded_sim()
    r = Rig(sim, tmp_path, TelegramMenuSection(operator_name="  Desk Owner  "))
    await r.publish()
    await r.press("c:pause")
    await r.confirm_last()
    (cmd,) = sim.repos.commands.pending()
    assert cmd.operator == "Desk Owner"


async def test_an_expired_confirmation_does_nothing(rig: Rig) -> None:
    await rig.press("c:kill")
    yes = [b for b in buttons(rig.tg.last) if b.startswith("y:")][0]
    rig.clock.t += 61.0
    await rig.press(yes)
    assert "expired" in rig.tg.last["text"]
    assert rig.sim.repos.commands.pending() == [] and not rig.sim.bot.stack.kill_switch.active


async def test_a_yes_pressed_twice_queues_one_command(rig: Rig) -> None:
    await rig.press("c:kill")
    yes = [b for b in buttons(rig.tg.last) if b.startswith("y:")][0]
    await rig.press(yes)
    await rig.press(yes)
    assert len(rig.sim.repos.commands.pending()) == 1


@pytest.mark.parametrize("data", [
    "c:live", "c:autonomy", "c:risk", "c:config", "c:restart", "c:capital", "v:../../.env", "REQUEST_LIVE",
    "y:deadbeef", "x:deadbeef", "y:DEADBEEF", "c:kill;c:reset", "c:kill ", "y:" + "a" * 60, "",
])
async def test_forged_button_data_is_never_trusted(rig: Rig, data: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        await rig.press(data)
    assert rig.sim.repos.commands.pending() == []
    assert not rig.sim.bot.stack.kill_switch.active and rig.sim.bot.mode.mode.value == "PAPER"
    assert all("Yes" not in json.dumps(m) for m in rig.tg.sent)


async def test_exit_choice_rechecks_that_the_position_is_still_open(rig: Rig) -> None:
    await rig.press("c:exit")
    choice = [b for b in buttons(rig.tg.last) if b.startswith("x:")][0]
    await rig.press(choice)
    yes = [b for b in buttons(rig.tg.last) if b.startswith("y:")][0]
    (trade,) = rig.sim.bot.stack.positions.open_trades()
    await rig.sim.bot.stack.positions.exit_trade(trade.trade_uid, ExitReasonManual())  # closed meanwhile
    await rig.sim.bot.settle()
    await rig.press(yes)
    assert "no longer open" in rig.tg.last["text"] and rig.sim.repos.commands.pending() == []


# --------------------------------------------------------------------------- #
# Reset rules
# --------------------------------------------------------------------------- #
async def test_reset_is_refused_while_the_feed_is_down_or_not_yet_stable(rig: Rig) -> None:
    ks = rig.sim.bot.stack.kill_switch
    await rig.press("c:reset")
    assert "Reset refused: the kill switch is not active" in rig.tg.last["text"]

    ks.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")
    await rig.sim.bot.settle()
    rig.conn.update(websocket_connected=False, websocket_state="RECONNECTING")
    await rig.publish(1)
    await rig.press("c:reset")
    assert "Reset refused: feed not connected (RECONNECTING)" in rig.tg.last["text"]
    assert not [b for b in buttons(rig.tg.last) if b.startswith("y:")]

    rig.conn.update(websocket_connected=True, websocket_state="CONNECTED", reconnects=1)
    await rig.publish(1)
    await rig.publish(30)
    await rig.press("c:reset")
    assert "stable for only 30s" in rig.tg.last["text"]

    rig.conn.update(websocket_stale=True)  # connected but stale is not stable
    await rig.publish(100)
    await rig.press("c:reset")
    assert "Reset refused" in rig.tg.last["text"]
    assert rig.sim.repos.commands.pending() == [] and ks.active

    rig.conn.update(websocket_stale=False)  # stable again, but the bot stops publishing (frozen heartbeat)
    await rig.stable_feed()
    rig.wall.now += timedelta(seconds=30)
    await rig.press("c:reset")
    assert "feed state unknown" in rig.tg.last["text"] and ks.active


async def test_feed_dropping_between_yes_and_the_run_refuses_the_reset(rig: Rig) -> None:
    ks = rig.sim.bot.stack.kill_switch
    ks.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")
    await rig.sim.bot.settle()
    await rig.stable_feed()
    await rig.press("c:reset")
    await rig.confirm_last()
    rig.conn.update(websocket_connected=False, websocket_state="RECONNECTING")
    await rig.publish(1)  # the control plane sees the drop before it runs the queued reset
    await rig.run_queue()
    (cmd,) = telegram_commands(rig.sim)
    assert cmd.status is ControlCommandStatus.REJECTED and "reset refused: feed not connected" in (cmd.result_message
                                                                                                   or "")
    assert ks.active and rig.tg.last["text"].startswith("❌")


async def test_a_telegram_reset_queued_by_hand_is_rechecked_by_the_control_plane(rig: Rig) -> None:
    rig.sim.bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")
    await rig.sim.bot.settle()
    rig.conn.update(websocket_connected=False)
    await rig.publish(1)
    rec = await run(rig.sim, enqueue(rig.sim, ControlCommandType.RESET_KILL_SWITCH, note="x",
                                     payload={"source": "telegram"}))
    assert rec.status is ControlCommandStatus.REJECTED and rig.sim.bot.stack.kill_switch.active


async def test_reset_accepted_on_a_stable_feed_reactivates_strategies_like_the_dashboard(rig: Rig) -> None:
    sim = rig.sim
    ks = sim.bot.stack.kill_switch
    ks.trigger(KillReason.WEBSOCKET_DISCONNECT, "market data WebSocket down for 60s")
    await sim.bot.settle()
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}
    await rig.stable_feed()
    await rig.press("c:reset")
    assert "WEBSOCKET_DISCONNECT since" in rig.tg.last["text"] and "stable for" in rig.tg.last["text"]
    await rig.confirm_last()
    await rig.run_queue()
    (cmd,) = telegram_commands(sim)
    assert cmd.status is ControlCommandStatus.DONE, cmd.result_message
    assert not ks.active
    assert set(statuses(sim).values()) == {StrategyStatus.ACTIVE}
    react = [e for e in sim.repos.risk_events.recent(100) if e.event_type == REACTIVATED_EVENT]
    assert {e.strategy_id for e in react} == {"orb_options", "vwap_ema"}
    assert all(e.details_json["operator"] == "@desk_owner" for e in react)
    resets = [e for e in sim.repos.risk_events.recent(100) if e.event_type == "KILL_SWITCH_RESET"]
    assert resets and resets[0].details_json["operator"] == "@desk_owner"
    assert rig.tg.last["text"].startswith("✅ Reset kill switch: kill switch reset for GLOBAL by @desk_owner")


async def test_strategy_scoped_kills_are_left_to_the_dashboard(rig: Rig) -> None:
    rig.sim.bot.stack.kill_switch.trigger(KillReason.REPEATED_REJECTIONS, "3 rejections", strategy_id="orb_options")
    await rig.sim.bot.settle()
    await rig.stable_feed()
    await rig.press("c:reset")
    assert "reset those from the dashboard" in rig.tg.last["text"]


# --------------------------------------------------------------------------- #
# Owner choice A: recovery message, never an automatic reset
# --------------------------------------------------------------------------- #
async def test_websocket_disconnect_recovery_message_after_two_stable_minutes(
        rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    sim = rig.sim
    ks = sim.bot.stack.kill_switch
    rig.conn.update(websocket_connected=False, websocket_state="RECONNECTING")
    await rig.publish(1)
    ks.trigger(KillReason.WEBSOCKET_DISCONNECT, "market data WebSocket down for 60s")
    await sim.bot.settle()
    assert not await rig.menu.check_recovery()  # still down

    rig.conn.update(websocket_connected=True, websocket_state="CONNECTED", reconnects=1)
    await rig.publish(1)
    for _ in range(11):
        await rig.publish(10)  # 110 s
    assert not await rig.menu.check_recovery()
    rig.conn.update(reconnects=2)  # a reconnect restarts the two minutes
    await rig.publish(10)
    for _ in range(11):
        await rig.publish(10)
    assert not await rig.menu.check_recovery()
    await rig.publish(10)  # 120 s since the last reconnect
    sent_before = len(rig.tg.sent)
    assert await rig.menu.check_recovery()
    msg = rig.tg.last
    n = sim.bot.stack.positions.open_trade_count()
    assert msg["text"] == f"Feed is back and stable. Positions: {n}. Tap Reset to resume"
    assert "c:reset" in buttons(msg)
    assert not await rig.menu.check_recovery()  # once per kill
    assert len(rig.tg.sent) == sent_before + 1
    for _ in range(5):
        await rig.publish(60)
        await rig.menu.check_recovery()
    assert ks.active and ks.global_event is not None  # the bot never resets itself
    assert sim.repos.commands.pending() == []


async def test_no_recovery_message_for_other_kill_reasons(rig: Rig) -> None:
    rig.sim.bot.stack.kill_switch.trigger(KillReason.MANUAL, "dashboard (ops): stop")
    await rig.sim.bot.settle()
    await rig.stable_feed(300)
    assert not await rig.menu.check_recovery()


async def test_kill_alert_carries_the_reason_and_is_always_sent_to_telegram(rig: Rig) -> None:
    from monitoring.telegram import TelegramAlertSink

    rig.sim.bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "market data WebSocket down for 60s")
    kill = [a for a in rig.sim.bot.stack.alerts.alerts if "Kill switch triggered" in a.title]
    assert kill and kill[-1].message.startswith("WEBSOCKET_DISCONNECT: market data WebSocket down for 60s")
    assert "until a named human resets it" in kill[-1].message
    assert TelegramAlertSink(token=TOKEN, chat_id=CHAT).wants(kill[-1])  # CRITICAL: always sent


# --------------------------------------------------------------------------- #
# Telegram down / slow / failing never affects trading
# --------------------------------------------------------------------------- #
async def test_telegram_down_does_not_affect_trading(tmp_path: Path) -> None:
    sim = Sim(at(9, 14))
    sim.bot.control._wall = Wall()
    await sim.bot.prepare()
    sim.bot.start()
    r = Rig(sim, tmp_path)
    r.tg.down = True
    r.menu.watch_interval = 0.001
    await r.menu.start()
    from tests.unit.test_bot_integration import ORB_DAY

    await sim.run_path(ORB_DAY)  # the entry opens while Telegram is unreachable
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    sim.bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")  # alert + recovery checks
    await sim.bot.settle()
    await r.stable_feed()
    await asyncio.sleep(0.05)
    assert r.menu.poll_errors > 0
    assert all(not t.done() for t in r.menu._tasks)  # still running, not crashed
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED  # the kill's exit ran
    assert sim.bot.stack.positions.net_quantities() == {}
    assert not await r.menu.check_recovery()  # cannot send: retried on the next check, never raises
    await r.menu.aclose()
    assert all(t.done() for t in r.menu._tasks) or not r.menu._tasks


async def test_a_failing_handler_is_contained(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.sim.bot.control.last_state = {"connections": "garbage"}
    with caplog.at_level(logging.ERROR):
        await rig.menu.safe_handle(press_update("v:status"))
    assert any("telegram menu update failed" in r.getMessage() for r in caplog.records)


async def test_a_rejected_token_stops_polling_without_retrying(tmp_path: Path) -> None:
    sim = await traded_sim()
    r = Rig(sim, tmp_path)
    r.api._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={})))
    await r.menu.start()
    await asyncio.sleep(0.05)
    assert r.menu.disabled == "HTTP 401"
    await r.menu.aclose()


async def test_an_instant_empty_poll_never_becomes_a_busy_loop(rig: Rig) -> None:
    """A server or proxy that answers getUpdates at once (not holding the long poll) must not starve trading."""
    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0.001)

    rig.menu._sleep = recording_sleep
    rig.menu.watch_interval = 10.0
    await rig.menu.start()
    ticks = 0
    for _ in range(20):  # the trading loop keeps getting turns
        await asyncio.sleep(0.001)
        ticks += 1
    await rig.menu.aclose()
    assert ticks == 20
    assert 1.0 in sleeps  # waited after an instant empty poll
    assert rig.tg.methods.count("getUpdates") <= len(sleeps) + 2


async def test_old_updates_are_skipped_at_start(rig: Rig) -> None:
    rig.tg.updates = [press_update("c:kill"), text_update("menu")]
    await rig.menu.drain_backlog()
    assert await rig.menu.poll_once() == 0
    assert rig.tg.sent == [] and rig.sim.repos.commands.pending() == []
    rig.tg.updates = [text_update("menu")]
    assert await rig.menu.poll_once() == 1 and rig.tg.sent


async def test_button_presses_are_rate_limited(rig: Rig) -> None:
    await rig.press("v:status")
    before = len(rig.tg.sent)
    await rig.menu.handle(press_update("v:risk"))  # same instant: too fast
    assert len(rig.tg.sent) == before and "Too many presses" in rig.tg.answers[-1]["text"]
    for _ in range(25):  # sustained presses: capped per minute
        rig.clock.t += 1.5
        await rig.menu.handle(press_update("v:status"))
    assert rig.menu.limited >= 2
    assert len(rig.tg.sent) - before <= TelegramMenuSection().max_presses_per_minute


# --------------------------------------------------------------------------- #
# Dashboard and Telegram at the same time: the control queue decides
# --------------------------------------------------------------------------- #
async def test_dashboard_and_telegram_reset_at_once_runs_one_reset(rig: Rig) -> None:
    sim = rig.sim
    sim.bot.stack.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, "feed lost")
    await sim.bot.settle()
    await rig.stable_feed()
    await rig.press("c:reset")
    await rig.confirm_last()
    CommandClient(sim.repos, "ops.lead", now_fn=rig.wall).reset_kill("feed back, checked")
    await rig.run_queue()
    results = sorted(c.status.value for c in sim.repos.commands.recent(10)
                     if c.command is ControlCommandType.RESET_KILL_SWITCH)
    assert results == ["DONE", "REJECTED"]
    react = [e for e in sim.repos.risk_events.recent(100) if e.event_type == REACTIVATED_EVENT]
    assert sorted(e.strategy_id for e in react) == ["orb_options", "vwap_ema"]  # once each


async def test_dashboard_and_telegram_exit_at_once_sells_once(rig: Rig) -> None:
    sim = rig.sim
    (trade,) = sim.bot.stack.positions.open_trades()
    await rig.press("c:exit")
    await rig.press([b for b in buttons(rig.tg.last) if b.startswith("x:")][0])
    await rig.confirm_last()
    CommandClient(sim.repos, "ops.lead", now_fn=rig.wall).exit_position(trade.trade_uid)
    await rig.run_queue()
    sells = [o for o in sim.repos.orders.list_for_trade(trade.trade_uid) if o.side is OrderSide.SELL]
    rec = sim.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED
    assert sim.bot.stack.positions.net_quantities() == {}  # never oversold
    assert sum(o.filled_quantity for o in sells) == rec.quantity  # one exit, not two
    statuses_ = sorted(c.status.value for c in sim.repos.commands.recent(10)
                       if c.command is ControlCommandType.EXIT_POSITION)
    assert len(statuses_) == 2  # both ran through the queue; the second found nothing left to sell


# --------------------------------------------------------------------------- #
# Transport and launcher wiring
# --------------------------------------------------------------------------- #
def test_parse_update_reads_messages_and_presses_and_rejects_junk() -> None:
    m = parse_update(text_update("menu"))
    assert m is not None and m.chat_id == CHAT and m.text == "menu" and m.user_name == "@desk_owner"
    p = parse_update(press_update("v:status", user={"id": 1, "first_name": "Desk", "last_name": "Owner"}))
    assert p is not None and p.callback_data == "v:status" and p.user_name == "Desk Owner"
    for junk in ({}, {"update_id": "x"}, {"update_id": 1}, {"update_id": 1, "message": {"chat": {}}},
                 {"update_id": 1, "edited_message": {"chat": {"id": 1}}}):
        assert parse_update(junk) is None


async def test_the_token_is_only_in_the_url_and_never_in_messages() -> None:
    tg = FakeTelegram()
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return tg(req)

    api = TelegramBotApi(token=TOKEN, chat_id=CHAT, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await api.send(f"leak {TOKEN}")
    assert TOKEN not in tg.last["text"]
    assert all(str(r.url).startswith("https://api.telegram.org/bot") and r.url.host == "api.telegram.org"
               for r in seen)
    assert all(TOKEN.encode() not in r.content for r in seen)


def test_menu_api_only_when_telegram_is_configured(tmp_path: Path) -> None:
    from tests.paper_support import paper_cfg

    assert default_menu_api(paper_cfg(tmp_path)) is None
    cfg = paper_cfg(tmp_path, telegram_bot_token=SecretStr(TOKEN), telegram_chat_id=CHAT)
    api = default_menu_api(cfg)
    assert isinstance(api, TelegramBotApi)
    off = cfg.model_copy(update={"settings": cfg.settings.model_copy(update={"alerts": cfg.settings.alerts.model_copy(
        update={"telegram": cfg.settings.alerts.telegram.model_copy(update={
            "menu": TelegramMenuSection(enabled=False)})})})})
    assert default_menu_api(off) is None


async def test_the_paper_session_starts_and_stops_the_menu(tmp_path: Path) -> None:
    from runtime.paper import PaperSession
    from tests.fakes import FakeWallClock
    from tests.paper_support import FakeConnections, paper_cfg

    def session(cfg: Any, wall: Any, conns: Any, menu_api: Any) -> PaperSession:
        return PaperSession(cfg, connections=conns, now_fn=wall, install_signal_handlers=False, run_scheduler=False,
                            menu_api=menu_api)

    tg = FakeTelegram()
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(WALL.replace(hour=9, minute=10))
    conns = FakeConnections(wall)

    def factory(_cfg: Any) -> TelegramBotApi:
        return TelegramBotApi(token=TOKEN, chat_id=CHAT, http=httpx.AsyncClient(transport=httpx.MockTransport(tg)))

    s = session(cfg, wall, conns, menu_api=factory)
    await s.start()
    assert s.telegram_menu is not None and "menu on" in s._alert_channels()["telegram_menu"]
    await asyncio.sleep(0.01)
    assert "getUpdates" in tg.methods  # outbound long-polling
    await s.shutdown()
    assert all(t.done() for t in s.telegram_menu._tasks) or not s.telegram_menu._tasks

    def broken(_cfg: Any) -> TelegramBotApi:
        raise RuntimeError("menu factory failed")

    s2 = session(paper_cfg(tmp_path / "b"), FakeWallClock(WALL.replace(hour=9, minute=10)),
                 FakeConnections(FakeWallClock(WALL.replace(hour=9, minute=10))), menu_api=broken)
    await s2.start()  # the session still starts
    assert s2.telegram_menu is None and s2.bot is not None
    await s2.shutdown()


# --------------------------------------------------------------------------- #
# Architecture: no LIVE / risk / config path from Telegram
# --------------------------------------------------------------------------- #
MENU_FILE = PROJECT_ROOT / "runtime" / "telegram_menu.py"
TRANSPORT_FILE = PROJECT_ROOT / "monitoring" / "telegram.py"


def test_only_five_commands_can_be_queued_from_telegram() -> None:
    assert {ControlCommandType.ENGINE_PAUSE, ControlCommandType.ENGINE_START,
                                ControlCommandType.RESET_KILL_SWITCH, ControlCommandType.KILL_SWITCH,
                                ControlCommandType.EXIT_POSITION} == ALLOWED_COMMANDS
    tree = ast.parse(MENU_FILE.read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "ControlCommandType"}
    assert used == {c.value for c in ALLOWED_COMMANDS}


def test_no_live_risk_config_or_credential_path_is_reachable_from_telegram() -> None:
    text = MENU_FILE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]  # skip the module docstring, which names what is NOT reachable
    for forbidden in ("REQUEST_LIVE", "REQUEST_PAPER", "SET_AUTONOMY", "request_live", "request_paper",
                      "set_autonomy", "evaluate_live", "LIVE_TRADING", "hard_limits", "TradingMode",
                      "get_secret_value", "telegram_bot_token", "EnvSettings", "load_config", "config.loader",
                      "cfg.risk", "allocated_capital", "scheduler", "request_stop", "shutdown", "sys.exit",
                      "subprocess", ".env", "token", "write_text", "write_bytes", "unlink", "rmtree",
                      "place_order", "cancel_order", "modify_order", ".broker", "mode_controller"):
        assert forbidden not in body, forbidden
    assert not re.search(r"\bos\.|\bimport os\b|\bsys\.", body)
    tree = ast.parse(text)
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    project = {m for m in imported if m.split(".")[0] in {"broker", "config", "core", "data", "execution", "learning",
                                                           "ml", "monitoring", "options", "risk", "runtime",
                                                           "strategies", "backtest", "dashboard", "research"}}
    assert project <= {"config.settings", "core.clock", "core.enums", "core.masking", "data.storage.records",
                       "monitoring.json_logger", "monitoring.telegram", "risk.kill_switch", "runtime.bot"}, project
    # Every button the menu can show is one of its fixed ids.
    for row in menu_buttons():
        for _, data in row:
            assert BUTTON_ID.fullmatch(data)


def test_telegram_transport_is_outbound_long_polling_only() -> None:
    text = TRANSPORT_FILE.read_text(encoding="utf-8")
    assert '"getUpdates"' in text
    for forbidden in ("setWebhook", "start_server", "socket", "bind(", "listen(", "aiohttp", "uvicorn"):
        assert forbidden not in text, forbidden
    paper = (PROJECT_ROOT / "runtime" / "paper.py").read_text(encoding="utf-8")
    assert "TelegramMenu(bot, api" in paper  # the menu gets the bot and the transport, nothing else
