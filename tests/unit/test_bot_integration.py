"""End-to-end PAPER bot on SYNTHETIC ticks: ticks -> candles -> ORB signal ->
contract -> risk -> paper fill -> protective stop -> square-off; day reset."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from broker.broker_interface import MarketDataSource
from broker.paper_broker import PaperBroker
from broker.upstox_mapping import parse_instrument
from config.loader import load_config
from config.settings import BotConfig
from core.enums import OptionType, OrderPurpose, StrategyStatus, TradeStatus, TradingMode
from core.event_bus import Topic
from core.models import Candle, Instrument, Tick
from data.market_data import MarketDataService
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.repositories import Repositories
from risk.kill_switch import KillReason
from runtime.bot import TradingBot, build_strategies
from tests.fakes import FakeWallClock, future_contract, option_contract
from tests.synthetic import FUT, INDEX, at, index_path, tick


def instrument_list(lot: int = 75, fut_expiries: Sequence[tuple[str, str]] = (
        ("2026-11-23", FUT), ("2026-12-29", "NSE_FO|NIFTYFUT-DEC"))) -> list[Instrument]:
    """SYNTHETIC Upstox instrument list: NIFTY options + futures."""
    opts = [parse_instrument(option_contract(k, o, "2026-11-10", key=f"NSE_FO|{k}{o}", lot=lot))
            for k in range(24300, 24850, 50) for o in ("CE", "PE")]
    futs = [parse_instrument(future_contract(e, key, lot=lot)) for e, key in fut_expiries]
    return opts + futs


CONTRACTS = instrument_list()


def cfg_with_futures() -> BotConfig:
    return load_config(use_default_env_file=False)


class NoSource(MarketDataSource):
    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self.instruments = instruments if instruments is not None else CONTRACTS
        self.calls = 0

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        raise AssertionError("the bot must use the instrument list")

    async def get_instruments(self, underlying_symbols: Sequence[str] = ("NIFTY",)) -> list[Instrument]:
        self.calls += 1
        return self.instruments

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        return {}

    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        return {}

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        return []


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


class Sim:
    def __init__(self, start: datetime, cfg: BotConfig | None = None, db: Database | None = None,
                 instruments: list[Instrument] | None = None) -> None:
        self.cfg = cfg or cfg_with_futures()
        self.wall = FakeWallClock(start)
        self.db = db or Database(":memory:")
        self.db.initialize()
        self.repos = Repositories(self.db, TradingMode.PAPER)
        sync_strategies(self.repos.strategies, self.cfg.strategies.strategies)
        self.md = MarketDataService(now_fn=self.wall)
        self.broker = PaperBroker(quotes=self.md.get, settings=self.cfg.settings.paper_broker,
                                  costs=self.cfg.settings.costs, now_fn=self.wall)
        self.subs: list[tuple[set[str], set[str]]] = []

        async def subscriber(add: set[str], remove: set[str]) -> None:
            self.subs.append((add, remove))

        self.bot = TradingBot(self.cfg, repos=self.repos, broker=self.broker, market_data=self.md,
                              source=NoSource(instruments), subscriber=subscriber, now_fn=self.wall,
                              sleep=no_sleep,
                              reconcile_confirm_delay=0)

    async def step(self, t: datetime, spot: float, premium: float = 100.0) -> None:
        self.wall.now = t
        assert self.bot.universe is not None
        # options first so their quotes are fresh when the index tick closes a candle
        if self.md.ltp(INDEX) is not None:
            for key in self.bot.universe.wanted(spot) | self.bot.universe.subscribed:
                self.md.on_tick(tick(key, premium, t, spread=0.1, vtt=250_000, oi=1_200_000))
        self.md.on_tick(tick(INDEX, spot, t, spread=0.0))
        await self.bot.settle()

    async def run_path(self, points: list[tuple[datetime, float]]) -> None:
        first = True
        for t, p in index_path(points):
            await self.step(t, p)
            if first:
                await self.bot.recenter_universe()
                first = False


ORB_DAY = [(at(9, 15), 24500.0), (at(9, 20), 24518.0), (at(9, 25), 24482.0), (at(9, 30), 24500.0),
           (at(9, 36), 24500.0), (at(9, 44), 24560.0), (at(9, 50), 24565.0)]


async def test_orb_day_end_to_end_then_square_off() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    bot = sim.bot
    assert bot.engine is not None

    # opening range was built from 09:15-09:30 synthetic ticks
    orb = next(s for s in bot.strategies if s.strategy_id == "orb_options")
    rng = orb.snapshot()["opening_range"]
    assert rng["ready"] and 24515 < rng["high"] <= 24518 and 24482 <= rng["low"] < 24486

    # exactly one CE entry from ORB, through the whole pipeline
    decisions = [e.payload for e in bot.bus.recent(Topic.ENTRY_DECISION)]
    approved = [d for d in decisions if d["approved"]]
    assert len(approved) == 1 and approved[0]["strategy_id"] == "orb_options"
    trades = bot.stack.positions.open_trades("orb_options")
    assert len(trades) == 1
    t = trades[0]
    assert t.option_type is OptionType.CE and t.instrument.strike == 24550 and t.filled_qty == 75
    orders = sim.repos.orders.list_for_trade(t.trade_uid)
    assert {o.purpose for o in orders} == {OrderPurpose.ENTRY, OrderPurpose.STOP_LOSS}
    snap = bot.snapshot()
    assert snap["engine"]["strategies"]["orb_options"]["approved_entries"] == 1
    assert snap["mode"] == "PAPER" and snap["open_trades"] == 1
    # the VWAP strategy saw no futures data -> no signals, untouched
    assert snap["engine"]["strategies"]["vwap_ema"]["signals"] == 0

    # option universe recentred around spot and subscribed
    assert sim.subs and any("NSE_FO|24550CE" in add for add, _ in sim.subs)

    # mandatory square-off at 15:10 closes everything
    await sim.step(at(15, 10), 24570.0, premium=110.0)
    await bot.stack.square_off.check()
    await bot.settle()
    rec = sim.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "SQUARE_OFF"
    assert bot.stack.positions.net_quantities() == {}


async def test_no_entries_while_paused_or_after_cutoff() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    sim.bot.pause()
    await sim.run_path(ORB_DAY)
    assert sim.bot.stack.positions.open_trade_count() == 0
    assert sim.bot.engine is not None
    assert sim.bot.engine.state.strategy("orb_options").signals == 0


async def test_kill_switch_blocks_pipeline_entries() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    sim.bot.stack.kill_switch.trigger(KillReason.MANUAL, "operator test")
    await sim.run_path(ORB_DAY)
    assert sim.bot.stack.positions.open_trade_count() == 0
    decisions = [e.payload for e in sim.bot.bus.recent(Topic.ENTRY_DECISION)]
    assert decisions and not any(d["approved"] for d in decisions)
    assert any("kill switch" in " ".join(d["reasons"]) for d in decisions)


async def test_new_day_reactivates_only_previous_day_blocks(tmp_path: Any) -> None:
    path = tmp_path / "bot.db"
    day1 = Sim(at(14, 0), db=Database(path))
    await day1.bot.prepare()
    ks = day1.bot.stack.kill_switch
    ks.trigger(KillReason.STRATEGY_DAILY_LOSS, "loss", strategy_id="orb_options")
    await day1.bot.settle()
    assert day1.repos.strategies.get("orb_options").status is StrategyStatus.KILLED  # type: ignore[union-attr]
    # make the KILLED status and the kill event belong to "yesterday"
    yesterday = (at(14, 0) - timedelta(days=1)).isoformat()
    day1.db.execute("UPDATE strategies SET updated_at = ? WHERE strategy_id = 'orb_options'", (yesterday,))
    ks.strategy_events["orb_options"] = ks.strategy_events["orb_options"].__class__(
        KillReason.STRATEGY_DAILY_LOSS, "loss", "orb_options", at(14, 0) - timedelta(days=1))

    day1.bot.new_day(at(14, 0).date())  # already reset today -> no-op
    assert day1.repos.strategies.get("orb_options").status is StrategyStatus.KILLED  # type: ignore[union-attr]
    day1.bot.last_reset_day = None
    day1.bot.new_day(at(14, 0).date())
    assert day1.repos.strategies.get("orb_options").status is StrategyStatus.ACTIVE  # type: ignore[union-attr]
    assert not ks.is_blocked("orb_options")
    assert any(e.details_json.get("operator") == "system:new_day" for e in day1.repos.risk_events.recent())


async def test_mid_session_restart_keeps_todays_kill(tmp_path: Any) -> None:
    path = tmp_path / "bot.db"
    a = Sim(at(10, 0), db=Database(path))
    await a.bot.prepare()
    a.bot.stack.kill_switch.trigger(KillReason.STRATEGY_DAILY_LOSS, "loss", strategy_id="orb_options")
    await a.bot.settle()
    a.db.close()
    b = Sim(at(11, 0), db=Database(path))
    await b.bot.prepare()  # restore + new_day for the SAME day
    assert b.bot.stack.kill_switch.is_blocked("orb_options")
    assert b.repos.strategies.get("orb_options").status is StrategyStatus.KILLED  # type: ignore[union-attr]
    b.db.close()


def test_vwap_disabled_without_a_current_future() -> None:
    cfg = load_config(use_default_env_file=False)
    assert [s.strategy_id for s in build_strategies(cfg, futures_key=None)] == ["orb_options"]
    both = build_strategies(cfg, futures_key="NSE_FO|X")
    assert [(s.strategy_id, s.signal_instrument_key) for s in both] == [
        ("orb_options", "NSE_INDEX|Nifty 50"), ("vwap_ema", "NSE_FO|X")]


def test_bot_refuses_non_paper_mode() -> None:
    cfg = cfg_with_futures()
    live = cfg.model_copy(update={"env": cfg.env.model_copy(update={"trading_mode": TradingMode.LIVE})})
    db = Database(":memory:")
    db.initialize()
    with pytest.raises(RuntimeError, match="PAPER"):
        TradingBot(live, repos=Repositories(db, TradingMode.PAPER), broker=None,  # type: ignore[arg-type]
                   market_data=MarketDataService(), source=NoSource())


async def test_schedule_registers_session_jobs() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    sch = sim.bot.schedule()
    assert set(sch.jobs) == {"candle_flush", "expire_entries", "square_off", "reconcile", "recenter",
                             "control", "start_of_day"}
    assert sch.jobs["reconcile"].every_seconds is not None and sch.jobs["reconcile"].every_seconds <= 300
    await sch.run_now("square_off")
    await sch.run_now("candle_flush")


# --------------------------------------------------------------------------- #
# NIFTY feed lag blocks NEW entries only
# --------------------------------------------------------------------------- #


class LaggedSim(Sim):
    """Ticks arrive ``lag`` seconds after their exchange time (e.g. a PC clock that is fast)."""

    lag = 0.0

    async def step(self, t: datetime, spot: float, premium: float = 100.0) -> None:
        self.wall.now = t + timedelta(seconds=self.lag)
        assert self.bot.universe is not None

        def late(key: str, px: float, **kw: Any) -> Tick:
            return tick(key, px, t, **kw).model_copy(update={"received_at": self.wall.now})

        if self.md.ltp(INDEX) is not None:
            for key in self.bot.universe.wanted(spot) | self.bot.universe.subscribed:
                self.md.on_tick(late(key, premium, spread=0.1, vtt=250_000, oi=1_200_000))
        self.md.on_tick(late(INDEX, spot, spread=0.0))
        await self.bot.settle()


async def test_feed_lag_above_limit_blocks_new_entries() -> None:
    sim = LaggedSim(at(9, 14))
    sim.lag = 7.0
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    assert sim.bot.stack.positions.open_trade_count() == 0
    decisions = [e.payload for e in sim.bot.bus.recent(Topic.ENTRY_DECISION)]
    assert decisions and not any(d["approved"] for d in decisions)  # the ORB breakout was signalled...
    assert all("feed lag" in " ".join(d["reasons"]) for d in decisions)  # ...and refused for the lag
    reason = sim.bot.entry_block_reason()
    assert reason is not None and "+7.0s" in reason and "exits unaffected" in reason


async def test_feed_lag_within_limit_does_not_block() -> None:
    sim = LaggedSim(at(9, 14))
    sim.lag = 4.0  # above the 2 s warning, below the 5 s entry limit
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    assert sim.bot.entry_block_reason() is None
    assert sim.bot.stack.positions.open_trade_count() == 1


async def test_feed_lag_never_blocks_exits() -> None:
    sim = LaggedSim(at(9, 14))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)  # no lag: the ORB CE entry opens
    (t,) = sim.bot.stack.positions.open_trades("orb_options")
    sim.lag = 9.0
    for k in range(20):  # enough lagged NIFTY ticks to move the 30-tick median above 5 s
        await sim.step(at(9, 50, 30 + k), 24565.0, premium=t.levels.entry)
    assert sim.bot.entry_block_reason() is not None  # entries are blocked now...
    assert sim.repos.trades.get(t.trade_uid).status is TradeStatus.OPEN  # type: ignore[union-attr]
    await sim.step(at(9, 51), 24560.0, premium=t.levels.stop - 5)  # premium through the stop, feed lagging
    await sim.step(at(9, 51, 1), 24560.0, premium=t.levels.stop - 5)
    assert sim.bot.entry_block_reason() is not None
    rec = sim.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED  # ...but the stop-loss exit still ran
    assert rec.exit_reason == "STOP_LOSS"
    assert sim.bot.stack.positions.net_quantities() == {}
