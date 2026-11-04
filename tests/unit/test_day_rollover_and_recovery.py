"""Day roll-over and recovery (Phase 4 audit fixes), on SYNTHETIC ticks:

* a position that could not be exited keeps the kill switch active and the
  strategies KILLED into the next day, across a restart, until a named human
  resets it
* a failed 09:00 instrument-list refresh retries, alerts and blocks entries
* per-direction entry caps survive a mid-session restart
* strategy status timestamps follow the bot's (injected) clock
* the start-of-day job reconciles after clearing the previous day's blocks
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from broker.errors import BrokerError
from core.clock import IST
from core.enums import OptionType, OrderSide, RiskSeverity, StrategyStatus
from core.event_bus import Topic
from core.models import OrderAck, OrderRequest
from data.storage.database import Database
from risk.kill_switch import DAY_SCOPED_REASONS, KillReason
from risk.stop_loss_manager import ExitReason
from tests.synthetic import INDEX, at, candle, opening_range_candles
from tests.unit.test_bot_integration import ORB_DAY, Sim

DAY2 = date(2026, 11, 4)


def day2(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 4, h, m, s, tzinfo=IST)


def shift_days(path: list[tuple[datetime, float]], days: int) -> list[tuple[datetime, float]]:
    return [(t + timedelta(days=days), p) for t, p in path]


def refuse_exits(sim: Sim) -> None:
    """The (paper) exchange rejects every SELL: positions cannot be exited."""
    real = sim.broker.place_order

    async def place(req: OrderRequest) -> OrderAck:
        if req.side is OrderSide.SELL:
            raise BrokerError("exchange rejected the exit order")
        return await real(req)

    sim.broker.place_order = place  # type: ignore[method-assign]


def statuses(sim: Sim) -> dict[str, StrategyStatus]:
    return {r.strategy_id: r.status for r in sim.repos.strategies.list_all()}


def decisions(sim: Sim) -> list[dict[str, Any]]:
    return [e.payload for e in sim.bot.bus.recent(Topic.ENTRY_DECISION)]


# --------------------------------------------------------------------------- #
# 1. Stuck position across the day boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", ["exit_failure", "square_off_incomplete"])
async def test_stuck_position_keeps_kill_and_strategies_killed_next_day(tmp_path: Any, failure: str) -> None:
    path = tmp_path / "bot.db"
    sim = Sim(at(9, 14), db=Database(path))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    refuse_exits(sim)
    ks = sim.bot.stack.kill_switch

    if failure == "exit_failure":
        await sim.bot.stack.positions.exit_all(ExitReason.MANUAL, "orb_options")
    else:
        await sim.step(at(15, 10), 24570.0)
        await sim.bot.stack.square_off.check()
        await sim.bot.settle()
        sim.wall.now = at(15, 21)
        await sim.bot.stack.square_off.check()
    await sim.bot.settle()

    assert ks.active and ks.global_event is not None
    assert ks.global_event.reason not in DAY_SCOPED_REASONS
    assert sim.bot.stack.positions.open_trade_count() == 1  # still stuck
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}

    # --- next trading day, bot kept running: the 09:00 job must NOT clear it ---
    sim.wall.now = day2(9, 0)
    await sim.bot.start_of_day(DAY2)
    assert ks.active, "a kill for a stuck position must survive the day roll-over"
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}
    still = [a for a in sim.bot.stack.alerts.alerts if a.title.startswith("Kill switch still active")]
    assert still and still[-1].severity is RiskSeverity.CRITICAL

    # no new entries on day 2
    await sim.run_path(shift_days(ORB_DAY, 1))
    assert sim.bot.stack.positions.open_trade_count() == 1
    approved = [d for d in decisions(sim) if d["approved"]]
    assert len(approved) == 1 and len(decisions(sim)) > 1  # day 1's entry only; day 2's signal was refused
    assert any("kill switch" in " ".join(d["reasons"]) for d in decisions(sim) if not d["approved"])

    # --- restart on day 2: the kill and the KILLED statuses are restored ---
    sim.db.close()
    restarted = Sim(day2(9, 5), db=Database(path))
    await restarted.bot.prepare()
    rks = restarted.bot.stack.kill_switch
    assert rks.active and rks.global_event is not None and rks.global_event.reason not in DAY_SCOPED_REASONS
    assert set(statuses(restarted).values()) == {StrategyStatus.KILLED}
    assert trade.trade_uid in {t.trade_uid for t in restarted.bot.stack.positions.open_trades()}

    # --- only a named human clears it ---
    assert rks.reset(operator="ops.lead", note="position closed manually at the broker")
    assert not rks.active
    restarted.db.close()


async def test_daily_loss_kill_still_clears_next_day(tmp_path: Any) -> None:
    sim = Sim(at(10, 0), db=Database(tmp_path / "bot.db"))
    await sim.bot.prepare()
    sim.bot.stack.kill_switch.trigger(KillReason.GLOBAL_DAILY_LOSS, "loss 10,000")
    await sim.bot.settle()
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED}
    sim.wall.now = day2(9, 0)
    await sim.bot.start_of_day(DAY2)
    assert not sim.bot.stack.kill_switch.active
    assert set(statuses(sim).values()) == {StrategyStatus.ACTIVE}
    sim.db.close()


# --------------------------------------------------------------------------- #
# 2. Failed 09:00 instrument-list refresh
# --------------------------------------------------------------------------- #


async def test_start_of_day_download_failure_retries_alerts_and_blocks_entries() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    source: Any = sim.bot.source
    working = source.get_instruments
    calls = 0

    async def down(*_: Any) -> Any:
        nonlocal calls
        calls += 1
        raise BrokerError("instrument list download failed: ConnectTimeout")

    source.get_instruments = down
    sim.wall.now = day2(9, 0)
    await sim.bot.start_of_day(DAY2)

    assert calls == 5  # retried (default 5 attempts, backoff between them)
    alerts = [a for a in sim.bot.stack.alerts.alerts if a.title == "Instrument list refresh failed"]
    assert len(alerts) == 1 and alerts[0].severity is RiskSeverity.CRITICAL
    assert "new entries are blocked" in alerts[0].message
    assert sim.bot.last_reset_day == DAY2  # the day still rolled
    reason = sim.bot.instrument_block_reason()
    assert reason is not None and "not loaded for 2026-11-04" in reason

    # safe state: a valid ORB setup on day 2 does not trade
    await sim.run_path(shift_days(ORB_DAY, 1))
    assert sim.bot.stack.positions.open_trade_count() == 0
    blocked = [d for d in decisions(sim) if any("instrument list not loaded" in r for r in d["reasons"])]
    assert blocked and not any(d["approved"] for d in decisions(sim))

    # recovers once the list is reachable again
    source.get_instruments = working
    await sim.bot.start_of_day(DAY2)
    assert sim.bot.instrument_block_reason() is None


async def test_bot_blocks_entries_when_instrument_list_is_from_a_previous_day() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    assert sim.bot.instrument_block_reason() is None
    sim.wall.now = day2(9, 20)  # start-of-day never ran (e.g. job missed)
    assert "not loaded for 2026-11-04" in (sim.bot.instrument_block_reason() or "")


# --------------------------------------------------------------------------- #
# 3. Per-direction cap survives a mid-session restart
# --------------------------------------------------------------------------- #


async def test_restart_after_orb_entry_keeps_per_direction_cap(tmp_path: Any) -> None:
    path = tmp_path / "bot.db"
    a = Sim(at(9, 14), db=Database(path))
    await a.bot.prepare()
    a.bot.start()
    await a.run_path(ORB_DAY)
    (t,) = a.bot.stack.positions.open_trades("orb_options")
    assert t.option_type is OptionType.CE
    a.db.close()

    b = Sim(at(9, 50), db=Database(path))  # restart mid-session, same day
    await b.bot.prepare()
    orb = next(s for s in b.bot.strategies if s.strategy_id == "orb_options")
    assert orb.entries[OptionType.CE] == 1 and orb.entries[OptionType.PE] == 0

    # Replay a fresh range + inside close + upside breakout: ORB's cap is
    # 1 CE per day, so no second CE signal; a PE breakdown is still allowed.
    for c in opening_range_candles(INDEX, high=24520.0, low=24480.0):
        orb.on_candle(c)
    assert orb.on_candle(candle(INDEX, 5, at(9, 35), 24500, 24510, 24495, 24505)) == ()  # inside: re-arm
    assert orb.on_candle(candle(INDEX, 5, at(9, 40), 24505, 24540, 24505, 24535)) == ()  # CE capped
    pe = orb.on_candle(candle(INDEX, 5, at(9, 45), 24480, 24480, 24455, 24460))
    assert len(pe) == 1 and pe[0].option_type is OptionType.PE
    b.db.close()


# --------------------------------------------------------------------------- #
# 4. Strategy status timestamps follow the injected clock
# --------------------------------------------------------------------------- #


async def test_strategy_status_timestamps_use_the_bot_clock(tmp_path: Any) -> None:
    far = datetime(2031, 3, 4, 10, 0, tzinfo=IST)  # far from the real date, a Tuesday
    sim = Sim(far, db=Database(tmp_path / "bot.db"))
    sim.bot.load_instruments = _noop_load(sim)  # type: ignore[method-assign]
    sim.bot.stack.kill_switch.trigger(KillReason.STRATEGY_DAILY_LOSS, "loss", strategy_id="orb_options")
    await sim.bot.settle()
    rec = sim.repos.strategies.get("orb_options")
    assert rec is not None and rec.status is StrategyStatus.KILLED and rec.updated_at == far

    sim.bot.stack.guard.on_pause("vwap_ema", "3 consecutive losses")  # type: ignore[misc]
    vw = sim.repos.strategies.get("vwap_ema")
    assert vw is not None and vw.status is StrategyStatus.PAUSED and vw.updated_at == far

    # same simulated day: nothing is re-activated; next simulated day: both are
    sim.bot.new_day(far.date())
    assert set(statuses(sim).values()) == {StrategyStatus.KILLED, StrategyStatus.PAUSED}
    sim.wall.now = far + timedelta(days=1)
    sim.bot.new_day(sim.wall.now.date())
    assert set(statuses(sim).values()) == {StrategyStatus.ACTIVE}
    assert all(r.updated_at == sim.wall.now for r in sim.repos.strategies.list_all())
    sim.db.close()


def _noop_load(sim: Sim) -> Any:
    async def load(*_: Any, **__: Any) -> None:
        return None

    return load


# --------------------------------------------------------------------------- #
# 5. Reconciliation after start-of-day
# --------------------------------------------------------------------------- #


async def test_start_of_day_reconciles_after_clearing_previous_day_blocks() -> None:
    sim = Sim(at(10, 0))
    await sim.bot.prepare()
    ks = sim.bot.stack.kill_switch
    ks.trigger(KillReason.STRATEGY_DAILY_LOSS, "loss", strategy_id="orb_options")
    await sim.bot.settle()
    before = len(sim.bot.stack.reconciler.history)

    sim.wall.now = day2(9, 0)
    await sim.bot.start_of_day(DAY2)
    rec = sim.bot.stack.reconciler
    assert len(rec.history) == before + 1
    report = rec.history[-1]
    assert report.trigger == "start_of_day" and report.at == day2(9, 0) and report.ok
    assert not ks.is_blocked("orb_options")  # cleared by the roll-over that ran first


async def test_start_of_day_reconciliation_catches_a_broker_only_position() -> None:
    sim = Sim(at(10, 0))
    await sim.bot.prepare()
    # A position exists at the broker that the bot does not know about.
    from core.enums import OrderType, TradingMode
    from core.ids import new_client_order_id

    await sim.step(at(10, 1), 24500.0)
    await sim.step(at(10, 1, 5), 24500.0)
    inst = sim.bot.instruments["NSE_FO|24500CE"]
    await sim.broker.place_order(OrderRequest(client_order_id=new_client_order_id(TradingMode.PAPER, "orb_options"),
                                              mode=TradingMode.PAPER, strategy_id="orb_options",
                                              instrument_key=inst.instrument_key, side=OrderSide.BUY,
                                              quantity=inst.lot_size, order_type=OrderType.MARKET))
    await sim.step(at(10, 2), 24500.0)
    sim.wall.now = day2(9, 0)
    await sim.bot.start_of_day(DAY2)
    await sim.bot.settle()
    assert not sim.bot.stack.reconciler.history[-1].ok
    ks = sim.bot.stack.kill_switch
    assert ks.active and ks.global_event is not None
    assert ks.global_event.reason is KillReason.RECONCILIATION_MISMATCH
