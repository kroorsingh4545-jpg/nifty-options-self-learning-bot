"""Dashboard units without Streamlit: auth, command client, view models."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from core.clock import IST
from core.enums import ControlCommandType, OptionType, OrderSide, TradeStatus, TradingMode
from dashboard.auth import LOCKOUT_SECONDS, MAX_FAILURES, Credentials, LoginGate, check_credentials, credentials_from
from dashboard.commands import COMMAND_TTL, CommandClient
from dashboard.data_access import (
    RuntimeView,
    candles_frame,
    duration,
    ema_series,
    feed_lag_status,
    inr,
    performance,
    portfolio,
    price_status,
    runtime_view,
    trade_markers,
    vwap_series,
    websocket_status,
)
from data.storage.database import Database
from data.storage.records import TradeRecord
from data.storage.repositories import Repositories

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=IST)
CREDS = Credentials("ops.lead", SecretStr("correct-horse-battery"))


@pytest.fixture
def repos(tmp_path: Any) -> Repositories:
    db = Database(tmp_path / "dash.db")
    db.initialize()
    return Repositories(db, TradingMode.PAPER)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_credentials_compare_and_unconfigured() -> None:
    assert check_credentials("ops.lead", "correct-horse-battery", CREDS)
    assert not check_credentials("ops.lead", "wrong", CREDS)
    assert not check_credentials("OPS.LEAD", "correct-horse-battery", CREDS)
    assert credentials_from(None, None) is None and credentials_from("u", None) is None
    gate = LoginGate({}, None)
    ok, msg = gate.attempt("a", "b")
    assert not ok and "not configured" in msg


def test_login_lockout_after_repeated_failures() -> None:
    clock = {"t": 0.0}
    session: dict[str, Any] = {}
    gate = LoginGate(session, CREDS, clock=lambda: clock["t"])
    for _ in range(MAX_FAILURES - 1):
        assert gate.attempt("ops.lead", "nope")[0] is False
    ok, msg = gate.attempt("ops.lead", "nope")
    assert not ok and "Locked" in msg
    assert not gate.attempt("ops.lead", "correct-horse-battery")[0]  # locked even with the right password
    clock["t"] += LOCKOUT_SECONDS + 1
    assert gate.attempt("ops.lead", "correct-horse-battery")[0]
    assert gate.user == "ops.lead" and "correct-horse-battery" not in str(session)
    gate.logout()
    assert gate.user is None


# --------------------------------------------------------------------------- #
# Command client
# --------------------------------------------------------------------------- #


def test_command_client_validates_and_stamps_operator(repos: Repositories) -> None:
    with pytest.raises(ValueError):
        CommandClient(repos, "  ")
    c = CommandClient(repos, "ops.lead", now_fn=lambda: NOW)
    bad = c.request_live("enable live trading", "ops.lead")
    assert not bad.ok and "type exactly" in bad.message
    assert not c.request_live("ENABLE LIVE TRADING", " ").ok
    assert not c.reset_kill("  ").ok
    assert not c.exit_position("").ok
    assert not c.engine("explode").ok and not c.set_autonomy("YOLO").ok
    assert repos.commands.recent() == []  # nothing invalid was queued

    kill = c.kill()
    live = c.request_live("ENABLE LIVE TRADING", "ops.lead")
    assert kill.ok and live.ok
    by_type = {r.command: r for r in repos.commands.recent()}
    k = by_type[ControlCommandType.KILL_SWITCH]
    assert k.operator == "ops.lead" and k.expires_at is None  # an emergency stop never expires
    lv = by_type[ControlCommandType.REQUEST_LIVE]
    assert lv.expires_at == NOW + COMMAND_TTL and lv.payload_json["approved_by"] == "ops.lead"


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #


def trade(uid: str, sid: str, pnl: float | None, minutes: int, status: TradeStatus = TradeStatus.CLOSED) -> TradeRecord:
    t0 = NOW + timedelta(minutes=minutes)
    return TradeRecord(trade_uid=f"PAPER-T-{uid}", mode=TradingMode.PAPER, strategy_id=sid, strategy_version="1.0.0",
                       instrument_key="NSE_FO|1", trading_symbol="NIFTY 24500 CE", option_type=OptionType.CE,
                       strike=24500, expiry=NOW.date(), side=OrderSide.BUY, quantity=75, entry_price=100.0,
                       entry_time=t0, exit_price=100.0 if status is TradeStatus.CLOSED else None,
                       exit_time=t0 + timedelta(minutes=5) if status is TradeStatus.CLOSED else None,
                       initial_stop_loss=75.0, stop_loss=75.0, net_pnl=pnl, status=status, created_at=t0)


def test_performance_metrics() -> None:
    trades = [trade("a", "orb_options", 1000.0, 0), trade("b", "vwap_ema", -400.0, 10),
              trade("c", "orb_options", -600.0, 20), trade("d", "orb_options", 500.0, 30),
              trade("e", "vwap_ema", None, 40, TradeStatus.OPEN)]
    p = performance(trades)
    assert p.closed == 4 and p.wins == 2 and p.losses == 2
    assert p.win_rate == pytest.approx(0.5)
    assert p.profit_factor == pytest.approx(1500 / 1000)
    assert p.net_pnl == pytest.approx(500.0)
    assert p.max_drawdown == pytest.approx(-1000.0)  # peak 1000 -> trough 0
    assert p.by_strategy == {"orb_options": pytest.approx(900.0), "vwap_ema": pytest.approx(-400.0)}
    assert list(p.equity["cumulative"]) == [1000.0, 600.0, 0.0, 500.0]
    empty = performance([])
    assert empty.win_rate is None and empty.profit_factor is None and empty.max_drawdown == 0


def test_runtime_view_online_offline_and_portfolio(repos: Repositories) -> None:
    from config.loader import load_config

    cfg = load_config(use_default_env_file=False)
    assert not runtime_view(repos, NOW).online
    state = {"daily": {"global_loss": 1200.0, "global_limit": 10000.0,
                       "strategies": {"orb_options": {"realized": -500.0, "unrealized": 200.0}}},
             "funds": {"available_margin": 150000.0}, "capital": {"deployed": 7500.0}, "positions": [{}]}
    repos.runtime_state.publish(state, at=NOW)
    assert runtime_view(repos, NOW + timedelta(seconds=2)).online
    v = runtime_view(repos, NOW + timedelta(seconds=30))
    assert not v.online and v.age_seconds == pytest.approx(30)
    p = portfolio(cfg, v, performance([]), trades_today=3)
    assert p["total_capital"] == 200_000 and p["available_funds"] == 150000.0
    assert p["total_pnl"] == pytest.approx(-300.0) and p["daily_loss_remaining"] == pytest.approx(8800.0)
    assert p["open_positions"] == 1 and p["trades_today"] == 3


def test_chart_overlays_and_markers(repos: Repositories) -> None:
    rows = [[(NOW + timedelta(minutes=5 * i)).isoformat(), 100 + i, 101 + i, 99 + i, 100 + i, 1000]
            for i in range(25)]
    repos.runtime_state.publish({"candles": {"NSE_FO|FUT|5": rows}}, at=NOW)
    df = candles_frame(runtime_view(repos, NOW), "NSE_FO|FUT", 5)
    assert len(df) == 25
    ema = ema_series(df["close"].tolist(), 20)
    assert ema[:19] == [None] * 19 and ema[-1] is not None
    vwap = vwap_series(df)
    assert vwap[0] == pytest.approx(100.0) and vwap[-1] is not None
    marks = trade_markers([trade("a", "orb_options", 10.0, 7)], df)
    assert list(marks["kind"]) == ["entry", "exit"] and marks["price"].notna().all()
    far = trade("b", "orb_options", 10.0, 60 * 24)  # a day later: outside the plotted window
    assert trade_markers([far], df).empty


def test_formatting() -> None:
    assert inr(-1234.5, 2) == "-₹1,234.50" and inr(None) == "—"
    assert duration(65) == "1m 05s" and duration(3725) == "1h 02m" and duration(None) == "—"

def _view(conn: dict[str, Any], *, online: bool = True, market: dict[str, Any] | None = None) -> RuntimeView:
    return RuntimeView({"connections": conn, "market": market or {}}, 1.0 if online else 60.0)


def test_websocket_tile_uses_the_structured_state_not_the_text() -> None:
    text = "CONNECTED (16 subscriptions)"  # what the 2026-11-06 session published
    live = {"websocket": text, "websocket_connected": True, "websocket_stale": False, "subscriptions": 16,
            "last_message_age_seconds": 0.4}
    assert websocket_status(_view(live))[0] == "connected"
    assert websocket_status(_view({**live, "websocket_stale": True}))[0] == "stale"
    assert websocket_status(_view({**live, "websocket_connected": False, "websocket": "RECONNECTING (16 "
                                   "subscriptions)"}))[0] == "offline"
    assert websocket_status(_view(live, online=False))[0] == "unknown"
    # an older bot that publishes only the text
    assert websocket_status(_view({"websocket": text, "subscriptions": 16}))[0] == "connected"
    assert websocket_status(_view({"websocket": "IDLE (0 subscriptions)"}))[0] == "offline"


def test_price_and_feed_lag_tiles() -> None:
    market = {"index": {"ltp": 24881.65, "fresh": True, "age_seconds": 0.3, "snapshot": False},
              "future": {"ltp": 24943.4, "fresh": False, "age_seconds": 42.0, "snapshot": False,
                         "symbol": "NIFTY FUT 23 NOV 26"}}
    v = _view({"feed_lag_seconds": 7.22, "feed_lag_limit_seconds": 2.0}, market=market)
    assert price_status(v, "index") == ("24,881.65", "live · 0s ago")
    value, detail = price_status(v, "future")
    assert value == "24,943.40" and detail.startswith("STALE · 42s ago") and "NIFTY FUT" in detail
    assert price_status(_view({}), "index") == ("—", "no tick yet")
    lag, detail, high = feed_lag_status(v)
    assert lag == "+7.2s" and high and "PC clock" in detail
    assert feed_lag_status(_view({"feed_lag_seconds": 0.15, "feed_lag_limit_seconds": 2.0}))[2] is False
    assert feed_lag_status(_view({})) == ("—", "no live NIFTY ticks yet", False)
