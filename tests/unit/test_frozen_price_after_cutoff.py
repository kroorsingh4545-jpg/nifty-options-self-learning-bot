"""Frozen-price entry gate near the close (weekend-1 task 2).

The gate stays fully active while new entries are allowed; after the
``no_new_entries_after`` cut-off (14:45) a quiet index raises no block, so the
dashboard (``session.entry_block`` = ``TradingBot.entry_block_reason()``,
runtime/control.py) shows no "price unchanged" banner. Exits never consult it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from core.enums import TradeStatus
from tests.synthetic import INDEX, at, tick
from tests.unit.test_bot_integration import ORB_DAY, Sim

LIMIT = 60.0  # websocket.max_frozen_price_seconds default


async def frozen_from(start: datetime, seconds: int, sim: Sim | None = None) -> Sim:
    """NIFTY ticks every 5 s at one unchanged price from ``start`` for ``seconds``."""
    if sim is None:
        sim = Sim(start)
        await sim.bot.prepare()
    for s in range(0, seconds + 1, 5):
        sim.wall.now = start + timedelta(seconds=s)
        sim.md.on_tick(tick(INDEX, 24500.0, sim.wall.now, spread=0.0))
    return sim


def frozen_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "NIFTY price frozen" in r.getMessage()]


def test_the_cutoff_and_limit_this_file_assumes() -> None:
    sim = Sim(at(10, 0))
    assert sim.cfg.risk.time_rules.no_new_entries_after == at(14, 45).time()
    assert sim.cfg.settings.websocket.max_frozen_price_seconds == LIMIT


# --------------------------------------------------------------------------- #
# Before the cut-off: unchanged, still blocks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("start", [(9, 30), (12, 0), (14, 43)])
async def test_before_the_cutoff_a_frozen_price_still_blocks_new_entries(
        start: tuple[int, int], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        sim = await frozen_from(at(*start), 65)  # 14:43:00 + 65 s = 14:44:05, still before 14:45
        assert sim.wall.now.time() < at(14, 45).time()
        reason = sim.bot.entry_block_reason()
    assert reason is not None and "price unchanged" in reason and INDEX in reason
    assert len(frozen_messages(caplog)) == 1


async def test_one_second_before_the_cutoff_still_blocks() -> None:
    sim = await frozen_from(at(14, 43, 54), 65)  # last tick 14:44:59
    assert sim.wall.now == at(14, 44, 59)
    assert sim.bot.frozen_price_block_reason() is not None


# --------------------------------------------------------------------------- #
# After the cut-off: no block, no banner, no warning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("start", [(14, 45), (14, 50), (15, 10), (15, 25)])
async def test_after_the_cutoff_a_frozen_price_raises_no_block_or_banner(
        start: tuple[int, int], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        sim = await frozen_from(at(*start), 120)  # 15:25 + 120 s = 15:27, still market hours
        assert sim.bot.market_hours(sim.wall.now)
        assert sim.md.frozen_seconds(INDEX) is not None and sim.md.frozen_seconds(INDEX) > LIMIT  # truly frozen
        assert sim.bot.frozen_price_block_reason() is None
        block = sim.bot.entry_block_reason()  # what the dashboard banner shows
    assert block is None or "price unchanged" not in block
    assert frozen_messages(caplog) == []


async def test_an_episode_open_at_the_cutoff_ends_there_without_an_allowed_again_warning(
        caplog: pytest.LogCaptureFixture) -> None:
    sim = await frozen_from(at(14, 43, 30), 80)  # 14:44:50, frozen 80 s
    assert sim.bot.frozen_price_block_reason() is not None
    with caplog.at_level(logging.INFO):
        await frozen_from(at(14, 44, 55), 15, sim)  # same price on to 14:45:10
        assert sim.bot.frozen_price_block_reason() is None
        assert sim.bot.frozen_price_block_reason() is None  # logged once
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("frozen-price check ended: new entries are closed for today") == 1
    assert not any("new entries allowed again" in m for m in messages)  # entries are NOT allowed again


async def test_the_next_morning_the_check_is_active_again() -> None:
    sim = await frozen_from(at(15, 0), 120)
    assert sim.bot.frozen_price_block_reason() is None
    nxt = at(10, 0) + timedelta(days=1)  # tests.synthetic day + 1 is a weekday trading day
    assert sim.cfg.settings.market_session.is_trading_day(nxt.date())
    await frozen_from(nxt, 65, sim)
    assert sim.bot.frozen_price_block_reason() is not None


# --------------------------------------------------------------------------- #
# Exits are never blocked, before or after the cut-off
# --------------------------------------------------------------------------- #
async def _open_orb_trade() -> tuple[Sim, str]:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (trade,) = sim.bot.stack.positions.open_trades("orb_options")
    return sim, trade.trade_uid


async def test_stop_loss_runs_during_a_frozen_price_before_the_cutoff() -> None:
    sim, uid = await _open_orb_trade()
    t0 = at(9, 50)
    for s in range(5, 80, 5):
        await sim.step(t0 + timedelta(seconds=s), 24565.0, premium=100.0)
    assert sim.bot.frozen_price_block_reason() is not None  # entries blocked...
    await sim.step(t0 + timedelta(seconds=85), 24565.0, premium=70.0)
    rec = sim.repos.trades.get(uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"  # ...exit ran


async def test_stop_loss_runs_during_a_frozen_price_after_the_cutoff() -> None:
    sim, uid = await _open_orb_trade()
    t0 = at(14, 50)
    for s in range(0, 80, 5):
        await sim.step(t0 + timedelta(seconds=s), 24565.0, premium=100.0)
    assert sim.md.frozen_seconds(INDEX) is not None and sim.md.frozen_seconds(INDEX) > LIMIT
    assert sim.bot.frozen_price_block_reason() is None
    assert sim.repos.trades.get(uid).status is TradeStatus.OPEN  # type: ignore[union-attr]
    await sim.step(t0 + timedelta(seconds=85), 24565.0, premium=70.0)
    rec = sim.repos.trades.get(uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"


async def test_square_off_runs_during_a_frozen_price_after_the_cutoff() -> None:
    sim, uid = await _open_orb_trade()
    t0 = at(15, 8)
    for s in range(0, 120, 5):
        await sim.step(t0 + timedelta(seconds=s), 24570.0, premium=110.0)
    assert sim.md.frozen_seconds(INDEX) is not None and sim.md.frozen_seconds(INDEX) > LIMIT
    await sim.step(at(15, 10), 24570.0, premium=110.0)
    await sim.bot.stack.square_off.check()
    await sim.bot.settle()
    rec = sim.repos.trades.get(uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "SQUARE_OFF"
    assert sim.bot.stack.positions.net_quantities() == {}
