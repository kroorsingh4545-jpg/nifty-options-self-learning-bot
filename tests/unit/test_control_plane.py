"""Dashboard control plane (bot side), on SYNTHETIC ticks.

* PAPER restart keeps the paper broker's book (no phantom mismatch)
* a named reset clears the kill switch and explicitly re-activates strategies
* resets are refused for same-day daily-loss kills and failed reconciliation
* every command type, expiry, and the published state snapshot
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from core.clock import IST
from core.enums import (
    AutonomyLevel,
    ControlCommandStatus,
    ControlCommandType,
    OrderPurpose,
    OrderStatus,
    StrategyStatus,
    TradeStatus,
    TradingMode,
)
from data.storage.database import Database
from data.storage.records import ControlCommandRecord
from execution.operator_controls import REACTIVATED_EVENT
from risk.kill_switch import KillReason
from risk.stop_loss_manager import ExitReason
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY, Sim
from tests.unit.test_day_rollover_and_recovery import refuse_exits, statuses

WALL = datetime(2026, 11, 3, 10, 0, tzinfo=IST)


class Wall:
    def __init__(self, now: datetime = WALL) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def traded_sim(path: Any = None) -> Sim:
    sim = Sim(at(9, 14), db=Database(path) if path else None)
    sim.bot.control._wall = Wall()  # control-plane wall clock
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    assert sim.bot.stack.positions.open_trade_count("orb_options") == 1
    return sim


def enqueue(sim: Sim, command: ControlCommandType, *, operator: str = "ops.lead", note: str = "",
            strategy_id: str | None = None, target: str | None = None, payload: dict[str, Any] | None = None,
            expires_in: float | None = 60.0) -> int:
    wall: Any = sim.bot.control._wall
    now = wall()
    return sim.repos.commands.enqueue(ControlCommandRecord(
        mode=TradingMode.PAPER, command=command, operator=operator, note=note, strategy_id=strategy_id,
        target=target, payload_json=payload or {}, requested_at=now,
        expires_at=now + timedelta(seconds=expires_in) if expires_in is not None else None))


async def run(sim: Sim, cmd_id: int) -> ControlCommandRecord:
    await sim.bot.control.process_pending()
    await sim.bot.settle()
    rec = sim.repos.commands.get(cmd_id)
    assert rec is not None
    return rec


# --------------------------------------------------------------------------- #
# Warning 3: PAPER restart keeps the paper book
# --------------------------------------------------------------------------- #


async def test_paper_restart_restores_book_so_reconciliation_passes(tmp_path: Any) -> None:
    path = tmp_path / "bot.db"
    a = await traded_sim(path)
    (trade,) = a.bot.stack.positions.open_trades("orb_options")
    funds_before = await a.broker.get_funds()
    a.db.close()

    b = Sim(at(9, 55), db=Database(path))  # restart: brand-new PaperBroker
    await b.bot.prepare()
    report = b.bot.stack.reconciler.last
    assert report is not None and report.trigger == "startup" and report.ok, report
    assert not b.bot.stack.kill_switch.active
    positions = {p.instrument_key: p.quantity for p in await b.broker.get_positions() if p.quantity}
    assert positions == {trade.instrument.instrument_key: trade.open_qty}
    funds_after = await b.broker.get_funds()
    assert funds_after.notional_cash == pytest.approx(funds_before.notional_cash)

    # the restored resting protective stop still works: price falls through it
    stop_orders = [o for o in b.repos.orders.list_for_trade(trade.trade_uid) if o.purpose is OrderPurpose.STOP_LOSS]
    assert stop_orders and stop_orders[0].status is OrderStatus.OPEN
    restored = b.bot.stack.positions.trades[trade.trade_uid]
    await b.step(at(10, 0), 24480.0, premium=restored.stop - 5)
    await b.step(at(10, 0, 5), 24480.0, premium=restored.stop - 5)
    rec = b.repos.trades.get(trade.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"
    b.db.close()


def test_restore_book_only_at_startup() -> None:
    from broker.errors import BrokerError
    from broker.paper_broker import PaperBroker

    broker = PaperBroker(quotes=lambda _k: None)
    broker._orders["x"] = object()  # type: ignore[assignment]
    with pytest.raises(BrokerError, match="only allowed at startup"):
        broker.restore_book([])


# --------------------------------------------------------------------------- #
# Warning 2: named reset clears the kill switch and re-activates strategies
# --------------------------------------------------------------------------- #


async def test_named_reset_clears_kill_and_reactivates_strategies() -> None:
    sim = await traded_sim()
    real_place = sim.broker.place_order
    refuse_exits(sim)
    await sim.bot.stack.positions.exit_all(ExitReason.MANUAL)
    await sim.bot.settle()
    ks = sim.bot.stack.kill_switch
    assert ks.active and ks.global_event is not None and not ks.global_event.day_scoped
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}

    # the operator closes the position (exits work again), then resets
    sim.broker.place_order = real_place  # type: ignore[method-assign]
    exit_id = enqueue(sim, ControlCommandType.EXIT_POSITION, target=sim.bot.stack.positions.open_trades()[0].trade_uid)
    await sim.step(at(10, 1), 24560.0)
    assert (await run(sim, exit_id)).status is ControlCommandStatus.DONE
    assert sim.bot.stack.positions.open_trade_count() == 0

    cmd = enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, note="exit confirmed flat at broker")
    rec = await run(sim, cmd)
    assert rec.status is ControlCommandStatus.DONE, rec.result_message
    assert not ks.active
    assert set(statuses(sim).values()) == {StrategyStatus.ACTIVE}
    assert sorted(rec.result_json["reactivated"]) == ["orb_options", "vwap_ema"]
    events = [e for e in sim.repos.risk_events.recent(100) if e.event_type == REACTIVATED_EVENT]
    assert {e.strategy_id for e in events} == {"orb_options", "vwap_ema"}
    assert all(e.details_json["operator"] == "ops.lead" for e in events)
    resets = [e for e in sim.repos.risk_events.recent(100) if e.event_type == "KILL_SWITCH_RESET"]
    assert resets and resets[0].details_json["operator"] == "ops.lead"


async def test_reset_refused_for_same_day_daily_loss_kill() -> None:
    sim = await traded_sim()
    sim.bot.stack.kill_switch.trigger(KillReason.GLOBAL_DAILY_LOSS, "loss 10,000")
    await sim.bot.settle()
    rec = await run(sim, enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, note="please"))
    assert rec.status is ControlCommandStatus.REJECTED
    assert "cannot be reset on the day it happened" in (rec.result_message or "")
    assert sim.bot.stack.kill_switch.active


async def test_reset_refused_when_reconciliation_fails() -> None:
    sim = await traded_sim()
    sim.bot.stack.kill_switch.trigger(KillReason.MANUAL, "operator test")
    await sim.bot.settle()
    assert sim.bot.stack.positions.open_trade_count() == 0  # the kill flattened everything
    # ...but the broker reports a position the bot does not know about
    from broker.paper_broker import _Position

    sim.broker._positions["NSE_FO|24500PE"] = _Position("NSE_FO|24500PE", "NIFTY 24500 PE", quantity=75,
                                                        average_price=90.0)
    rec = await run(sim, enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, note="try"))
    assert rec.status is ControlCommandStatus.REJECTED
    assert "reconciliation failed" in (rec.result_message or "") and "POSITION_QTY" in (rec.result_message or "")
    assert sim.bot.stack.kill_switch.active
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}


async def test_reset_needs_a_note_and_an_active_scope() -> None:
    sim = await traded_sim()
    rec = await run(sim, enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, note="nothing active"))
    assert rec.status is ControlCommandStatus.REJECTED and "not active" in (rec.result_message or "")
    sim.bot.stack.kill_switch.trigger(KillReason.MANUAL, "x")
    await sim.bot.settle()
    rec = await run(sim, enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, note="  "))
    assert rec.status is ControlCommandStatus.REJECTED and "note" in (rec.result_message or "")
    with pytest.raises(ValidationError):
        enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, operator="   ", note="x")


async def test_strategy_reset_keeps_strategy_killed_while_global_kill_active() -> None:
    sim = await traded_sim()
    ks = sim.bot.stack.kill_switch
    ks.trigger(KillReason.MANUAL, "vwap only", strategy_id="vwap_ema")
    await sim.bot.settle()
    ks.trigger(KillReason.API_FAILURE, "upstox down")
    await sim.bot.settle()
    rec = await run(sim, enqueue(sim, ControlCommandType.RESET_KILL_SWITCH, strategy_id="vwap_ema",
                                 note="vwap fixed"))
    assert rec.status is ControlCommandStatus.DONE
    assert rec.result_json["reactivated"] == [] and "vwap_ema" in rec.result_json["still_blocked"]
    assert statuses(sim)["vwap_ema"] is StrategyStatus.KILLED


# --------------------------------------------------------------------------- #
# Other commands
# --------------------------------------------------------------------------- #


async def test_kill_command_halts_and_never_expires() -> None:
    sim = await traded_sim()
    wall: Any = sim.bot.control._wall
    cmd = enqueue(sim, ControlCommandType.KILL_SWITCH, note="dashboard emergency", expires_in=1)
    other = enqueue(sim, ControlCommandType.ENGINE_PAUSE, expires_in=1)
    wall.now = WALL + timedelta(minutes=5)  # both are past their expiry
    await sim.step(at(10, 1), 24560.0)
    kill = await run(sim, cmd)
    assert kill.status is ControlCommandStatus.DONE
    assert sim.repos.commands.get(other).status is ControlCommandStatus.EXPIRED  # type: ignore[union-attr]
    ks = sim.bot.stack.kill_switch
    assert ks.active and ks.global_event is not None and ks.global_event.reason is KillReason.MANUAL
    assert "ops.lead" in ks.global_event.message
    assert sim.bot.stack.positions.open_trade_count() == 0  # flattened through the execution layer


async def test_strategy_kill_command_is_isolated() -> None:
    sim = await traded_sim()
    rec = await run(sim, enqueue(sim, ControlCommandType.KILL_SWITCH, strategy_id="vwap_ema", note="x"))
    assert rec.status is ControlCommandStatus.DONE
    ks = sim.bot.stack.kill_switch
    assert ks.is_blocked("vwap_ema") and not ks.is_blocked("orb_options")
    assert sim.bot.stack.positions.open_trade_count("orb_options") == 1


async def test_manual_exit_command() -> None:
    sim = await traded_sim()
    (t,) = sim.bot.stack.positions.open_trades("orb_options")
    await sim.step(at(10, 1), 24560.0)
    rec = await run(sim, enqueue(sim, ControlCommandType.EXIT_POSITION, target=t.trade_uid, note="take it"))
    assert rec.status is ControlCommandStatus.DONE
    tr = sim.repos.trades.get(t.trade_uid)
    assert tr is not None and tr.status is TradeStatus.CLOSED and tr.exit_reason == "MANUAL"
    ev = next(e for e in sim.repos.risk_events.recent(50) if e.event_type == "OPERATOR_EXIT")
    assert ev.details_json["operator"] == "ops.lead"
    again = await run(sim, enqueue(sim, ControlCommandType.EXIT_POSITION, target=t.trade_uid))
    assert again.status is ControlCommandStatus.REJECTED


async def test_engine_commands_never_stop_protection() -> None:
    sim = await traded_sim()
    assert (await run(sim, enqueue(sim, ControlCommandType.ENGINE_STOP))).status is ControlCommandStatus.DONE
    assert sim.bot.engine is not None and sim.bot.engine.state.engine_state.value == "STOPPED"
    # protection keeps working while STOPPED: mandatory square-off still flattens
    await sim.step(at(15, 10), 24570.0, premium=110.0)
    await sim.bot.stack.square_off.check()
    await sim.bot.settle()
    assert sim.bot.stack.positions.open_trade_count() == 0
    assert (await run(sim, enqueue(sim, ControlCommandType.ENGINE_PAUSE))).status is ControlCommandStatus.DONE
    assert sim.bot.engine.state.engine_state.value == "PAUSED"
    assert (await run(sim, enqueue(sim, ControlCommandType.ENGINE_START))).status is ControlCommandStatus.DONE
    assert sim.bot.engine.state.engine_state.value == "RUNNING"


async def test_live_request_is_refused_with_every_reason() -> None:
    sim = await traded_sim()
    rec = await run(sim, enqueue(sim, ControlCommandType.REQUEST_LIVE,
                                 payload={"confirmation": "ENABLE LIVE TRADING", "approved_by": "ops.lead"}))
    assert rec.status is ControlCommandStatus.REJECTED
    reasons = rec.result_json["reasons"]
    assert "LIVE order routing is not available in this build" in reasons
    assert "PAPER validation not completed for this version" in reasons
    assert not any("type exactly" in r for r in reasons)
    assert sim.bot.mode.mode is TradingMode.PAPER
    wrong = await run(sim, enqueue(sim, ControlCommandType.REQUEST_LIVE, payload={"confirmation": "enable live"}))
    assert any("type exactly" in r for r in wrong.result_json["reasons"])
    assert sim.bot.mode.mode is TradingMode.PAPER
    paper = await run(sim, enqueue(sim, ControlCommandType.REQUEST_PAPER))
    assert paper.status is ControlCommandStatus.DONE


async def test_set_autonomy_and_bad_payload() -> None:
    sim = await traded_sim()
    ok = await run(sim, enqueue(sim, ControlCommandType.SET_AUTONOMY, payload={"level": "OBSERVE_ONLY"}))
    assert ok.status is ControlCommandStatus.DONE and sim.bot.mode.autonomy is AutonomyLevel.OBSERVE_ONLY
    bad = await run(sim, enqueue(sim, ControlCommandType.SET_AUTONOMY, payload={"level": "FULL_AUTO_LIVE"}))
    assert bad.status is ControlCommandStatus.FAILED


def test_processed_command_is_final(tmp_path: Any) -> None:
    db = Database(tmp_path / "x.db")
    db.initialize()
    from data.storage.repositories import Repositories

    repos = Repositories(db, TradingMode.PAPER)
    cid = repos.commands.enqueue(ControlCommandRecord(mode=TradingMode.PAPER, command=ControlCommandType.ENGINE_PAUSE,
                                                      operator="a"))
    assert repos.commands.complete(cid, ControlCommandStatus.DONE, "ok", at=WALL)
    assert not repos.commands.complete(cid, ControlCommandStatus.FAILED, "again", at=WALL)
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError, match="already processed"):
        db.execute("UPDATE control_commands SET status = 'PENDING' WHERE id = ?", (cid,))
    with pytest.raises(Exception, match="isolation|PAPER"):
        repos.commands.enqueue(ControlCommandRecord(mode=TradingMode.LIVE, command=ControlCommandType.ENGINE_PAUSE,
                                                    operator="a"))


# --------------------------------------------------------------------------- #
# Published state
# --------------------------------------------------------------------------- #


async def test_published_state_has_every_dashboard_field_and_no_secrets() -> None:
    sim = await traded_sim()
    token = "upstox-access-token-very-secret-123456"
    sim.cfg.env.__dict__["upstox_access_token"] = SecretStr(token)
    state = await sim.bot.control.publish_state()
    stored = sim.repos.runtime_state.latest()
    assert stored is not None and stored[0]["published_at"] == state["published_at"]
    text = json.dumps(stored[0])
    assert token not in text and "secret" not in text.lower()
    for key in ("mode", "autonomy", "engine_state", "session", "connections", "capital", "funds", "daily",
                "limits", "kill_switch", "strategies", "positions", "reconciliation", "live_preconditions",
                "candles", "events", "alerts"):
        assert key in state, key
    assert state["mode"] == "PAPER" and state["live_available"] is False
    (pos,) = state["positions"]
    for col in ("strategy_id", "symbol", "side", "quantity", "entry_price", "ltp", "unrealized", "stop_loss",
                "target", "trailing_stop", "r_multiple", "holding_seconds"):
        assert col in pos, col
    assert {s["strategy_id"] for s in state["strategies"]} == {"orb_options", "vwap_ema"}
    orb = next(s for s in state["strategies"] if s["strategy_id"] == "orb_options")
    assert orb["open_trades"] == 1 and orb["last_signal"] is not None and orb["allocated_capital"] == 100_000
    assert any(k.startswith("NSE_INDEX|Nifty 50|") for k in state["candles"])
    assert state["capital"]["deployed"] > 0
