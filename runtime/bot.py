"""TradingBot — builds and runs the whole PAPER system.

Startup sequence (``prepare``):
 1. warn (CRITICAL) if no NSE holidays are configured for the current year
 2. load the Upstox instrument list: NIFTY options (nearest expiry -> strike
    universe), the current-month NIFTY future, and ALL lot sizes
 3. restore daily loss guard, kill switch and open trades from SQLite
 4. build strategies (NIFTY_FUT strategies get the current-month future),
    resolver, gateway, engine; wire tick -> candle -> engine
 5. subscribe NIFTY index + current future; new-day reset (only clears
    statuses / kill events from PREVIOUS days)
 6. startup reconciliation (a mismatch triggers the kill switch)

Scheduled jobs (``schedule``), all IST:
 * candle flush (1 s)          * stale-entry expiry (5 s)
 * square-off check (5 s)      * strike-universe recenter (30 s)
 * periodic reconciliation (<= 300 s)
 * daily 09:00 start-of-day (Mon-Fri): refresh the instrument list, roll the
   futures contract after expiry, new-day reset
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, time
from typing import Any

from broker.broker_interface import Broker, MarketDataSource
from broker.paper_broker import PaperBroker
from config.settings import BotConfig
from core.clock import now_ist
from core.engine import TradingEngine
from core.enums import StrategyStatus, TradingMode
from core.event_bus import EventBus
from core.interfaces import PassThroughFilter, SignalFilter, StrategyProtocol
from core.mode_controller import ModeController
from core.models import Instrument
from core.scheduler import TradingScheduler
from core.state_manager import StateManager
from data.candle_builder import CandleBuilder
from data.instruments import InstrumentMaster, build_master
from data.market_data import MarketDataService
from data.storage.repositories import Repositories
from execution.stack import ExecutionStack, build_stack
from monitoring.json_logger import get_logger
from runtime.contracts import OptionContractResolver, OptionUniverse
from runtime.gateway import RiskEntryGateway
from strategies.base_strategy import BaseStrategy
from strategies.orb_options import OrbOptionsStrategy
from strategies.vwap_ema import VwapEmaStrategy

log = get_logger(__name__)

STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "orb_options": OrbOptionsStrategy,
    "vwap_ema": VwapEmaStrategy,
}

Subscriber = Callable[[set[str], set[str]], Awaitable[None]]  # (add, remove)


def signal_source(cfg: BotConfig, sid: str) -> str:
    return str(cfg.strategies.strategies[sid].params.get("signal_instrument", "NIFTY_INDEX"))


def build_strategies(cfg: BotConfig, now_fn: Callable[[], datetime] = now_ist, *,
                     futures_key: str | None = None) -> list[BaseStrategy]:
    """``futures_key`` is the current-month NIFTY future resolved from the
    Upstox instrument list; strategies that need it are skipped without it."""
    out: list[BaseStrategy] = []
    inst = cfg.settings.instruments
    for sid, scfg in cfg.strategies.strategies.items():
        cls = STRATEGY_CLASSES.get(sid)
        if cls is None:
            log.critical("no implementation for configured strategy; skipped", extra={"strategy_id": sid})
            continue
        source = signal_source(cfg, sid)
        key = futures_key if source == "NIFTY_FUT" else inst.nifty_index_key
        if not key:
            log.critical("signal instrument unavailable; strategy disabled",
                         extra={"strategy_id": sid, "signal_instrument": source,
                                "detail": "no current-month NIFTY future in the Upstox instrument list"})
            continue
        out.append(cls(sid, scfg, signal_instrument_key=key, now_fn=now_fn))
    return out


class TradingBot:
    def __init__(
        self,
        cfg: BotConfig,
        *,
        repos: Repositories,
        broker: Broker,
        market_data: MarketDataService,
        source: MarketDataSource,
        subscriber: Subscriber | None = None,
        signal_filter: SignalFilter | None = None,
        now_fn: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        scheduler: TradingScheduler | None = None,
        reconcile_confirm_delay: float = 1.0,
    ) -> None:
        if cfg.mode is not TradingMode.PAPER:
            raise RuntimeError("only PAPER mode can run in this build")
        self.cfg = cfg
        self.repos = repos
        self.broker = broker
        self.md = market_data
        self.source = source
        self._subscriber = subscriber
        self._now = now_fn
        self.bus = EventBus()
        self.stack: ExecutionStack = build_stack(cfg, repos=repos, broker=broker, market_data=market_data,
                                                 now_fn=now_fn, sleep=sleep,
                                                 reconcile_confirm_delay=reconcile_confirm_delay)
        self.mode = ModeController(live_available=False, autonomy=cfg.settings.app.autonomy_level, now_fn=now_fn)
        self.filter = signal_filter or PassThroughFilter()
        self.scheduler = scheduler or TradingScheduler()
        session = cfg.settings.market_session
        self.candles = CandleBuilder(session_open=session.market_open, session_close=session.market_close)
        self.strategies: list[BaseStrategy] = []
        self.universe: OptionUniverse | None = None
        self.engine: TradingEngine | None = None
        self.instruments: dict[str, Instrument] = {}
        self.master: InstrumentMaster | None = None
        self.futures: Instrument | None = None
        self.instrument_summary: dict[str, object] = {}
        self.last_reset_day: date | None = None
        self._resolver: OptionContractResolver | None = None

    # ------------------------------------------------------------------ #
    @property
    def spot_key(self) -> str:
        return self.cfg.settings.instruments.nifty_index_key

    def spot(self) -> float | None:
        return self.md.ltp(self.spot_key)

    def market_hours(self, at: datetime) -> bool:
        return self.cfg.settings.market_session.is_open(at)

    async def load_instruments(self, today: date, instruments: Sequence[Instrument] | None = None) -> None:
        """Load the Upstox instrument list: the ONLY source of lot sizes, tick
        sizes, freeze quantities, expiries and the current-month future."""
        insts = list(instruments) if instruments is not None else await self.source.get_instruments(("NIFTY",))
        master = build_master(insts, today)
        self.instrument_summary = master.validate_nifty(today)
        expiry = master.nearest_option_expiry(today)
        previous = self.universe.subscribed if self.universe is not None else set()
        self.universe = OptionUniverse(master.nifty_options(expiry), expiry=expiry,
                                       band=self.cfg.settings.instruments.strike_band)
        self.universe.subscribed = set(previous)
        self.futures = master.current_month_future(today)
        self.master = master
        self.instruments = dict(master.by_key)
        if isinstance(self.broker, PaperBroker):
            self.broker.register_instruments(master.by_key.values())
        if self._resolver is not None:
            self._resolver.universe = self.universe
        log.info("instrument list ready", extra=self.instrument_summary)

    async def prepare(self, instruments: Sequence[Instrument] | None = None) -> None:
        s = self.stack
        today = self._now().date()
        session = self.cfg.settings.market_session
        if not session.holidays_known_for(today.year):
            log.critical("no NSE trading holidays configured for this year; the bot cannot tell holidays apart",
                         extra={"year": today.year, "fix": "add NSE holidays to market_session.holidays"})
        await self.load_instruments(today, instruments)
        assert self.universe is not None and self.futures is not None
        s.guard.restore()
        s.kill_switch.restore()

        unrestorable = s.positions.restore(self.instruments)
        if unrestorable:
            log.critical("open trades could not be restored", extra={"trades": unrestorable})

        self.strategies = build_strategies(self.cfg, self._now, futures_key=self.futures.instrument_key)
        resolver = OptionContractResolver(universe=self.universe, strategies=dict(self.cfg.strategies.strategies),
                                          spot_fn=self.spot, quote_fn=self.md.get)
        self._resolver = resolver
        gateway = RiskEntryGateway(self.cfg, s, self.broker)
        strategies: list[StrategyProtocol] = list(self.strategies)
        self.engine = TradingEngine(strategies=strategies, resolver=resolver, gateway=gateway,
                                    signal_filter=self.filter, bus=self.bus,
                                    state=StateManager([x.strategy_id for x in self.strategies]),
                                    market_hours=self.market_hours, now_fn=self._now)
        self.md.add_listener(self.candles.on_tick)
        self.md.add_listener(self.engine.on_tick)
        self.candles.add_listener(self.engine.on_candle)
        await self._subscribe(self.base_keys(), set())
        self.new_day(today)
        await s.reconciler.reconcile("startup")
        log.info("bot prepared", extra={**self.instrument_summary,
                                        "strategies": [x.strategy_id for x in self.strategies]})

    def base_keys(self) -> set[str]:
        """Always-subscribed instruments: NIFTY index (+ current future)."""
        keys = {self.spot_key}
        if self.futures is not None:
            keys.add(self.futures.instrument_key)
        return keys

    async def _subscribe(self, add: set[str], remove: set[str]) -> None:
        if (add or remove) and self._subscriber is not None:
            await self._subscriber(add, remove)

    async def start_of_day(self, day: date, instruments: Sequence[Instrument] | None = None) -> None:
        """Daily pre-open job: refresh the instrument list (lot sizes may be
        revised; expiries move on), roll NIFTY_FUT strategies to the new
        current-month future after expiry, then the new-day reset."""
        old_future = self.futures.instrument_key if self.futures else None
        await self.load_instruments(day, instruments)
        assert self.futures is not None
        new_future = self.futures.instrument_key
        if old_future != new_future:
            log.warning("NIFTY futures contract rolled", extra={"from": old_future, "to": new_future,
                                                                "symbol": self.futures.trading_symbol})
            for strat in self.strategies:
                if signal_source(self.cfg, strat.strategy_id) == "NIFTY_FUT":
                    strat.set_signal_instrument(new_future)
            held = {t.instrument.instrument_key for t in self.stack.positions.open_trades()}
            remove = {old_future} - held if old_future else set()
            await self._subscribe({new_future}, remove)
        self.new_day(day)

    # ------------------------------------------------------------------ #
    def new_day(self, day: date) -> None:
        """New trading day. Clears only what belongs to PREVIOUS days: kill
        events, and PAUSED/KILLED strategy statuses last changed before today.
        Today's breaches (e.g. after a mid-session restart) stay in force."""
        if self.last_reset_day == day:
            return
        s = self.stack
        s.guard.roll_day_if_needed()
        s.kill_switch.roll_day(day)
        for rec in self.repos.strategies.list_all():
            scfg = self.cfg.strategies.strategies.get(rec.strategy_id)
            if scfg is None:
                continue
            if not scfg.enabled:
                if rec.status is not StrategyStatus.DISABLED:
                    self.repos.strategies.set_status(rec.strategy_id, StrategyStatus.DISABLED)
            elif rec.status in (StrategyStatus.PAUSED, StrategyStatus.KILLED) and rec.updated_at.date() < day:
                self.repos.strategies.set_status(rec.strategy_id, StrategyStatus.ACTIVE)
                log.info("strategy re-activated for new day", extra={"strategy_id": rec.strategy_id})
        if self.engine is not None:
            self.engine.new_day(day)
        self.last_reset_day = day

    async def recenter_universe(self) -> None:
        if self.universe is None:
            return
        spot = self.spot()
        if spot is None:
            return
        held = {t.instrument.instrument_key for t in self.stack.positions.open_trades()}
        add, remove = self.universe.recenter(spot, keep=held)
        await self._subscribe(add, remove)

    def schedule(self) -> TradingScheduler:
        s = self.stack
        ex = self.cfg.settings.execution
        sch = self.scheduler
        sch.every("candle_flush", 1, lambda: self.candles.flush(self._now()))
        sch.every("expire_entries", 5, s.orders.expire_stale_entries)
        sch.every("square_off", ex.square_off_check_seconds, s.square_off.check)
        sch.every("reconcile", ex.reconcile_interval_seconds, lambda: s.reconciler.reconcile("periodic"))
        sch.every("recenter", 30, self.recenter_universe)
        sch.daily("start_of_day", time(9, 0), lambda: self.start_of_day(self._now().date()))
        return sch

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.engine is None:
            raise RuntimeError("call prepare() first")
        self.engine.start()

    def pause(self) -> None:
        if self.engine is not None:
            self.engine.pause()

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.scheduler.shutdown()

    async def settle(self) -> None:
        for _ in range(3):
            if self.engine is not None:
                await self.engine.drain()
            await self.stack.settle()

    def snapshot(self) -> dict[str, Any]:
        s = self.stack
        return {
            "mode": self.mode.mode.value,
            "autonomy": self.mode.autonomy.value,
            "engine": self.engine.state.snapshot() if self.engine else None,
            "strategies": [x.snapshot() for x in self.strategies],
            "daily": s.guard.snapshot(),
            "kill_switch": s.kill_switch.status(),
            "session_phase": s.square_off.phase().value,
            "open_trades": s.positions.open_trade_count(),
            "instruments": self.instrument_summary,
        }
