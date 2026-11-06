"""Headless Streamlit tests of dashboard/app.py (AppTest) against a PAPER
bot on SYNTHETIC ticks. Commands clicked in the UI are processed by the real
bot control service, closing the loop UI -> queue -> bot -> state -> UI."""

from __future__ import annotations

import sqlite3
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from core.enums import ControlCommandStatus, ControlCommandType
from dashboard.auth import bind_is_local
from data.storage.database import SCHEMA_VERSION, Database
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY, Sim

APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"
USER, PASSWORD = "ops.lead", "correct-horse-battery"
TOKEN = "upstox-token-must-never-appear-0123456789"

REQUIRED_METRICS = {
    "Total capital", "Deployed capital", "Available funds", "Realized P&L", "Unrealized P&L", "Total P&L",
    "Daily loss used", "Daily loss remaining", "Open positions", "Trades today", "Win rate", "Profit factor",
    "Max drawdown", "Upstox", "WebSocket", "Market session", "Square-off in", "Autonomy", "Bot",
    "Global daily loss limit", "Per-strategy daily loss limit", "Per-trade risk", "Open exposure", "Margin usage",
    "WebSocket subscriptions", "API rate-limit usage", "ML / RL models", "Learning-loop candidates",
    "Pending approvals", "NIFTY index", "NIFTY future", "Feed lag",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_USERNAME", USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    st.cache_resource.clear()


@pytest.fixture
async def bot(tmp_path: Path) -> Any:
    sim = Sim(at(9, 14), db=Database(tmp_path / "bot.db"))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    await sim.bot.control.publish_state()
    yield sim
    sim.db.close()


def app(db: Path, *, signed_in: bool = True) -> AppTest:
    t = AppTest.from_file(str(APP), default_timeout=60)
    t.session_state["db_override"] = str(db)
    if signed_in:
        t.session_state["auth_user"] = USER
    return t.run()


def page_text(t: AppTest) -> str:
    parts: list[str] = []
    for kind in ("markdown", "caption", "error", "warning", "info", "success", "subheader", "title"):
        parts += [str(e.value) for e in getattr(t, kind)]
    parts += [f"{m.label} {m.value}" for m in t.metric]
    parts += [str(df.value) for df in t.dataframe]
    return "\n".join(parts)


def positions_df(t: AppTest) -> Any:
    return next(d.value for d in t.dataframe if "R-multiple" in d.value.columns)


def button(t: AppTest, label: str) -> Any:
    return next(b for b in t.button if b.label == label)


async def process(sim: Sim) -> None:
    await sim.bot.control.process_pending()
    await sim.bot.settle()
    await sim.bot.control.publish_state()


# --------------------------------------------------------------------------- #


def test_login_required_and_unconfigured_refuses(env: None, bot: Sim, monkeypatch: pytest.MonkeyPatch) -> None:
    t = app(bot.db.path, signed_in=False)
    assert "PAPER MODE" not in page_text(t)
    t.text_input(key="login_user").input(USER)
    t.text_input(key="login_password").input("wrong-password")
    t = button(t, "Sign in").click().run()
    assert any("Wrong username or password" in e.value for e in t.error)
    t.text_input(key="login_user").input(USER)
    t.text_input(key="login_password").input(PASSWORD)
    t = button(t, "Sign in").click().run()
    assert "PAPER MODE" in page_text(t)

    monkeypatch.delenv("DASHBOARD_USERNAME")
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    st.cache_resource.clear()
    t2 = app(bot.db.path, signed_in=False)
    assert any("not configured" in e.value for e in t2.error)


def test_paper_default_metrics_cards_positions_and_no_secrets(env: None, bot: Sim) -> None:
    t = app(bot.db.path)
    assert not t.exception, t.exception
    assert t.toggle(key="live_toggle").value is False  # PAPER by default
    text = page_text(t)
    assert "PAPER MODE" in text and "Mode: **PAPER**" in text
    labels = {m.label for m in t.metric}
    assert labels >= REQUIRED_METRICS, REQUIRED_METRICS - labels
    assert any(b.label == "🛑 EMERGENCY KILL SWITCH" for b in t.button)  # always visible
    assert "orb_options" in text and "vwap_ema" in text  # strategy cards
    assert any(b.label == "Kill orb_options" for b in t.button)
    positions = positions_df(t)
    assert len(positions) == 1 and {"Strategy", "Symbol", "Stop-loss", "R-multiple"} <= set(positions.columns)
    assert any(b.label == "Exit" for b in t.button)
    assert TOKEN not in text and PASSWORD not in text


def test_live_blocked_while_orders_are_pending(env: None, bot: Sim) -> None:
    t = app(bot.db.path)  # the open position has a resting protective stop order
    t = t.toggle(key="live_toggle").set_value(True).run()
    assert button(t, "Request LIVE").disabled
    assert any("pending order" in w.value for w in t.warning)


async def test_live_rejects_wrong_text_and_backend_refuses_live(env: None, tmp_path: Path) -> None:
    bot = Sim(at(9, 14), db=Database(tmp_path / "idle.db"))  # no positions, no pending orders
    await bot.bot.prepare()
    await bot.bot.control.publish_state()
    t = app(bot.db.path)
    t = t.toggle(key="live_toggle").set_value(True).run()
    assert "Switch to LIVE" in page_text(t)
    t.text_input(key="live_phrase").input("enable live")
    t.text_input(key="live_approver").input(USER)
    t = button(t, "Request LIVE").click().run()
    assert any("type exactly" in e.value for e in t.error)
    assert t.toggle(key="live_toggle").value is False  # back to PAPER
    assert not [c for c in bot.repos.commands.recent() if c.command is ControlCommandType.REQUEST_LIVE]

    t = t.toggle(key="live_toggle").set_value(True).run()
    t.text_input(key="live_phrase").input("ENABLE LIVE TRADING")
    t.text_input(key="live_approver").input(USER)
    t = button(t, "Request LIVE").click().run()
    (cmd,) = [c for c in bot.repos.commands.recent() if c.command is ControlCommandType.REQUEST_LIVE]
    assert cmd.operator == USER


async def test_live_refusal_reasons_shown(env: None, bot: Sim) -> None:
    from dashboard.commands import CommandClient

    CommandClient(bot.repos, USER).request_live("ENABLE LIVE TRADING", USER)
    await process(bot)
    t = app(bot.db.path)
    t = t.toggle(key="live_toggle").set_value(True).run()
    text = page_text(t)
    assert "LIVE order routing is not available in this build" in text
    assert "Mode: **PAPER**" in text


async def test_kill_switch_click_confirm_reset_and_reactivation(env: None, bot: Sim) -> None:
    t = app(bot.db.path)
    t = button(t, "🛑 EMERGENCY KILL SWITCH").click().run()
    assert any("Confirm the emergency stop" in w.value for w in t.warning)
    t = button(t, "Confirm kill switch").click().run()
    (kill,) = [c for c in bot.repos.commands.recent() if c.command is ControlCommandType.KILL_SWITCH]
    assert kill.operator == USER and kill.status is ControlCommandStatus.PENDING

    await bot.step(at(10, 0), 24560.0)
    await process(bot)
    assert bot.bot.stack.kill_switch.active and bot.bot.stack.positions.open_trade_count() == 0
    t = app(bot.db.path)
    assert any("Kill switch active" in e.value for e in t.error)
    assert any(b.label == "🛑 KILL SWITCH ACTIVE" for b in t.button)

    t.text_input(key="reset_note").input("checked flat at broker")
    t = button(t, "Reset kill switch").click().run()
    (reset,) = [c for c in bot.repos.commands.recent() if c.command is ControlCommandType.RESET_KILL_SWITCH]
    assert reset.operator == USER
    await process(bot)
    done = bot.repos.commands.get(reset.id)  # type: ignore[arg-type]
    assert done is not None and done.status is ControlCommandStatus.DONE, done.result_message
    assert not bot.bot.stack.kill_switch.active
    assert {r.status.value for r in bot.repos.strategies.list_all()} == {"ACTIVE"}
    t = app(bot.db.path)
    assert not any("Kill switch active" in e.value for e in t.error)


async def test_manual_exit_with_confirmation(env: None, bot: Sim) -> None:
    t = app(bot.db.path)
    t = button(t, "Exit").click().run()
    assert any("at market now?" in w.value for w in t.warning)
    t = button(t, "Confirm exit").click().run()
    (cmd,) = [c for c in bot.repos.commands.recent() if c.command is ControlCommandType.EXIT_POSITION]
    assert cmd.target and cmd.operator == USER
    await bot.step(at(10, 0), 24560.0)
    await process(bot)
    assert bot.bot.stack.positions.open_trade_count() == 0
    t = app(bot.db.path)
    assert len(positions_df(t)) == 0


def bot_created_db(path: Path) -> Path:
    db = Database(path)
    db.initialize()  # what the bot does at startup (the dashboard never does)
    db.close()
    return path


def test_offline_bot_is_obvious(env: None, tmp_path: Path) -> None:
    t = app(bot_created_db(tmp_path / "idle.db"))
    assert not t.exception, t.exception
    text = page_text(t)
    assert "PAPER MODE" in text and "OFFLINE" in text and "not publishing state" in text


# --------------------------------------------------------------------------- #
# Only the bot migrates; the dashboard refuses on any schema mismatch
# --------------------------------------------------------------------------- #


def refused(t: AppTest) -> str:
    return " ".join(e.value for e in t.error if "Refusing to start" in e.value)


def test_missing_database_is_refused_and_not_created(env: None, tmp_path: Path) -> None:
    path = tmp_path / "nothing.db"
    t = app(path)
    assert "does not exist" in refused(t) and "start the bot first" in refused(t)
    assert not path.exists()  # the dashboard created nothing
    assert "PAPER MODE" not in page_text(t)


@pytest.mark.parametrize(("version", "msg"), [(SCHEMA_VERSION - 1, "older than this code"),
                                             (SCHEMA_VERSION + 1, "newer than this code")])
def test_schema_version_mismatch_is_refused(env: None, tmp_path: Path, version: int, msg: str) -> None:
    path = bot_created_db(tmp_path / "bot.db")
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM schema_version")
    raw.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, '2026-11-04T09:00:00+05:30')",
                (version,))
    raw.commit()
    raw.close()
    t = app(path)
    assert msg in refused(t)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == version  # not migrated
    raw.close()


def test_schema_upgraded_under_a_running_dashboard_is_caught(env: None, tmp_path: Path) -> None:
    path = bot_created_db(tmp_path / "bot.db")
    t = app(path)
    assert not refused(t)
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, '2026-11-04T09:00:00+05:30')",
                (SCHEMA_VERSION + 1,))
    raw.commit()
    raw.close()
    assert "newer than this code" in refused(t.run())


# --------------------------------------------------------------------------- #
# Network exposure: loopback only unless explicitly allowed
# --------------------------------------------------------------------------- #


def test_shipped_streamlit_config_binds_loopback() -> None:
    cfg = tomllib.loads((APP.parents[1] / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["server"]["address"] == "127.0.0.1"


@pytest.mark.parametrize(("address", "local"), [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                                ("0.0.0.0", False), ("192.168.1.20", False), ("", False),  # noqa: S104
                                                (None, False)])
def test_bind_is_local(address: str | None, local: bool) -> None:
    assert bind_is_local(address) is local


@pytest.mark.parametrize("address", ["0.0.0.0", ""])  # noqa: S104 - checking that it is refused
def test_non_loopback_bind_is_refused(env: None, bot: Sim, monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    monkeypatch.setattr(st, "get_option", lambda key: address if key == "server.address" else None)
    t = app(bot.db.path)
    assert any("Refusing to run" in e.value and "127.0.0.1" in e.value for e in t.error)
    assert "PAPER MODE" not in page_text(t) and not any(b.label == "🛑 EMERGENCY KILL SWITCH" for b in t.button)


def test_remote_bind_needs_explicit_opt_in(env: None, bot: Sim, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "get_option", lambda key: "0.0.0.0" if key == "server.address" else None)  # noqa: S104
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--allow-remote"])
    t = app(bot.db.path)
    assert "PAPER MODE" in page_text(t)
