"""Event-driven backtester: the real PAPER bot replayed on historical candles.

The backtest runs the SAME code as a PAPER session (``runtime.TradingBot``):
candle builder, ORB and VWAP+EMA strategies, ML filter interface, contract
resolver, ``RiskManager`` gate, ``PositionManager`` / ``OrderManager``,
the paper broker's fill model and charges, stops / targets / trailing,
the daily loss guard, the kill switch, reconciliation and the 15:10 square-off.
Only the inputs differ:

* Clock: a ``SimClock`` injected everywhere as ``now_fn`` (strategies, risk,
  execution, broker, repositories), so every persisted timestamp is simulated
  time. It can only move forward.
* Market data: each 1-minute candle is replayed as four ticks at +0/+15/+30/+45 s
  following open -> low -> high -> close for an up-candle and
  open -> high -> low -> close for a down-candle (the stop of a long option is
  visited before its target when both are inside one candle). Options get a
  bid/ask from ``CostModel.quote``. Only instruments the bot has subscribed to
  receive ticks, like the real feed.
* Scheduled jobs: the bot's own job list (``TradingBot.schedule``) is run on
  simulated time (``SimulatedScheduler``), except the dashboard control plane,
  as background tasks like APScheduler (a slow job never blocks the feed and
  is never overlapped).
* Waiting: ``SimClock.sleep`` is the bot's injected ``sleep``. Exit retries and
  backoffs, order waits and the reconciliation debounce park until the replay
  has moved simulated time on, and meet the ticks that arrived meanwhile.
  Nothing in a backtest waits on the wall clock.
* Storage: a throwaway SQLite database (in memory by default). The configured
  trading database is refused.

No lookahead: ticks are emitted in time order with timestamps inside their
candle, so a candle is only complete (and visible to a strategy) once its
interval has ended; orders fill against the price at the moment they are
placed or later; the backtest only ever receives the days it is asked to run.
"""

from __future__ import annotations

import asyncio
import re
import time as wall_time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from backtest.cost_model import CostModel, Execution, SpreadModel
from backtest.data import BacktestDataError, HistoricalDataset
from broker.broker_interface import MarketDataSource
from broker.paper_broker import PaperBroker
from config.settings import AppSettings, BotConfig, StrategyDefaults
from core.clock import IST, ensure_ist
from core.enums import OrderSide, TradingMode
from core.event_bus import Topic
from core.models import Candle, Instrument, OrderUpdate, Tick
from core.scheduler import TradingScheduler
from data.market_data import MarketDataService
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.records import DailyPnlRecord, FillRecord, OrderRecord, RiskEventRecord, TradeRecord
from data.storage.repositories import Repositories
from ml.inference_service import StrategyModels
from ml.model_registry import LoadedModel
from monitoring.json_logger import get_logger
from runtime.bot import TradingBot

log = get_logger(__name__)

TICK_OFFSETS_SECONDS = (0, 15, 30, 45)
SKIPPED_JOBS = frozenset({"control"})  # dashboard command queue + state publishing: no operator in a backtest

#: Strategy config keys a backtest / walk-forward candidate may override.
#: Capital, risk limits and ML settings are never overridable here; sizing
#: only through the (reduce-only) multiplier.
OVERRIDABLE_STRATEGY_KEYS = frozenset({"enabled", "timeframe_minutes", "entry_window", "params", "strike_selection",
                                       "sizing", "stop_loss", "take_profit", "trailing"})
OVERRIDABLE_SIZING_KEYS = frozenset({"size_multiplier"})


class BacktestConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Simulated time
# --------------------------------------------------------------------------- #


class SimClock:
    """Injected ``now_fn`` and ``sleep``. Simulated time never goes backwards.

    ``sleep(seconds)`` parks the caller until the replay has advanced the
    clock by ``seconds``: exit retries, backoffs, order waits and the
    reconciliation debounce see the ticks that arrive in between, exactly as
    they would live, instead of all happening at one instant."""

    def __init__(self, start: datetime) -> None:
        self.now = ensure_ist(start)
        self._waiters: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = 0
        self.closed = False

    def __call__(self) -> datetime:
        return self.now

    def set(self, t: datetime) -> None:
        t = ensure_ist(t)
        if t < self.now:
            raise RuntimeError(f"simulated time cannot go backwards ({self.now.isoformat()} -> {t.isoformat()})")
        self.now = t
        self._wake()

    @property
    def sleepers(self) -> int:
        return sum(1 for *_, fut in self._waiters if not fut.done())

    def _wake(self) -> None:
        due = sorted((w for w in self._waiters if self.closed or w[0] <= self.now), key=lambda w: (w[0], w[1]))
        if not due:
            return
        self._waiters = [w for w in self._waiters if not (self.closed or w[0] <= self.now)]
        for *_, fut in due:
            if not fut.done():
                fut.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0 or self.closed:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        entry = (self.now + timedelta(seconds=seconds), self._seq, fut)
        self._waiters.append(entry)
        try:
            await fut
        finally:
            if entry in self._waiters:  # cancelled while parked
                self._waiters.remove(entry)

    def close(self) -> None:
        """End of the run: every later sleep returns at once."""
        self.closed = True
        self._wake()


async def settle_simulation(max_spins: int = 1_000_000) -> None:
    """Run the event loop until nothing is runnable: every background task
    has finished or is parked on ``SimClock.sleep``. (``bot.settle()`` would
    wait for parked tasks forever, because only the replay advances time.)
    Needs the standard asyncio loop, whose ready queue it inspects."""
    loop = asyncio.get_running_loop()
    ready = getattr(loop, "_ready", None)
    if ready is None:
        raise RuntimeError("backtests need the standard asyncio event loop")
    idle = 0
    for _ in range(max_spins):
        await asyncio.sleep(0)
        idle = idle + 1 if not ready else 0
        if idle >= 2:
            return
    raise RuntimeError("backtest background tasks did not settle")


class SimulatedScheduler:
    """Runs the bot's registered jobs on simulated time, as background tasks
    like APScheduler does (interval jobs at most once per step and never
    overlapping, coalesced; daily jobs once per weekday)."""

    def __init__(self, scheduler: TradingScheduler, skip: frozenset[str] = SKIPPED_JOBS) -> None:
        self.scheduler = scheduler
        self.skip = skip
        self._next: dict[str, datetime] = {}
        self._last_day: dict[str, date] = {}
        self._running: dict[str, asyncio.Task[None]] = {}

    def mark_daily_done(self, job_id: str, day: date) -> None:
        self._last_day[job_id] = day

    def _start(self, job_id: str) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(self.scheduler.run_now(job_id))
        self._running[job_id] = task
        return task

    async def run_due(self, now: datetime) -> list[asyncio.Task[None]]:
        started = []
        for job_id, spec in self.scheduler.jobs.items():
            if job_id in self.skip:
                continue
            running = self._running.get(job_id)
            if running is not None and not running.done():
                continue  # max_instances=1: a slow run is never overlapped
            if spec.every_seconds is not None:
                due = self._next.get(job_id)
                if due is None or now >= due:
                    self._next[job_id] = now + timedelta(seconds=spec.every_seconds)
                    started.append(self._start(job_id))
            elif spec.at is not None and now.weekday() < 5 and now.time() >= spec.at \
                    and self._last_day.get(job_id) != now.date():
                self._last_day[job_id] = now.date()
                started.append(self._start(job_id))
        return started


class HistoricalSource(MarketDataSource):
    """The instrument list from the dataset; no network, no live quotes."""

    def __init__(self, instruments: Sequence[Instrument]) -> None:
        self.instruments = list(instruments)

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        return [i for i in self.instruments if i.option_type is not None and (expiry is None or i.expiry == expiry)]

    async def get_instruments(self, underlying_symbols: Sequence[str] = ("NIFTY",)) -> list[Instrument]:
        return list(self.instruments)

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        return {}

    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        return {}

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        return []


# --------------------------------------------------------------------------- #
# Candle -> tick replay
# --------------------------------------------------------------------------- #


def candle_path(c: Candle) -> tuple[float, float, float, float]:
    """Intra-candle price path: up-candles visit the low first, down-candles the high."""
    if c.close >= c.open:
        return (c.open, c.low, c.high, c.close)
    return (c.open, c.high, c.low, c.close)


class DayReplay:
    """Ticks for one trading day, generated on demand per step."""

    def __init__(self, candles: Mapping[str, Sequence[Candle]], instruments: Mapping[str, Instrument],
                 cost: CostModel) -> None:
        self._inst = instruments
        self._cost = cost
        self._by_minute: dict[str, dict[datetime, Candle]] = {}
        self._cum_before: dict[str, dict[datetime, int]] = {}
        minutes: set[datetime] = set()
        for key, series in candles.items():
            by, cum, total = {}, {}, 0
            for c in series:
                by[c.start] = c
                cum[c.start] = total
                total += c.volume
                minutes.add(c.start)
            self._by_minute[key] = by
            self._cum_before[key] = cum
        self.steps = [m + timedelta(seconds=s) for m in sorted(minutes) for s in TICK_OFFSETS_SECONDS]

    def keys(self) -> set[str]:
        return set(self._by_minute)

    def _order(self, key: str) -> int:
        inst = self._inst.get(key)
        if inst is not None and inst.option_type is not None:
            return 0  # option quotes first, so they are current when an index/futures tick closes a candle
        return 1 if inst is not None else 2

    def ticks(self, t: datetime, keys: set[str]) -> list[Tick]:
        minute = t.replace(second=0, microsecond=0)
        idx = TICK_OFFSETS_SECONDS.index(t.second)
        out: list[Tick] = []
        for key in sorted(keys, key=lambda k: (self._order(k), k)):
            c = self._by_minute.get(key, {}).get(minute)
            if c is None:
                continue
            price = candle_path(c)[idx]
            vol_share = (0, c.volume // 3, 2 * c.volume // 3, c.volume)[idx]
            vtt = self._cum_before[key][minute] + vol_share
            inst = self._inst.get(key)
            if inst is not None and inst.option_type is not None:
                bid, ask = self._cost.quote(price, inst.tick_size)
                depth = self._cost.depth(inst.lot_size)
                out.append(Tick(instrument_key=key, ltp=price, exchange_time=t, received_at=t, bid=bid, ask=ask,
                                bid_qty=depth, ask_qty=depth, volume_today=vtt, oi=c.oi))
            else:
                out.append(Tick(instrument_key=key, ltp=price, exchange_time=t, received_at=t,
                                volume_today=vtt if inst is not None else None, oi=c.oi))
        return out


# --------------------------------------------------------------------------- #
# Config helpers (always re-validated: hard limits cannot be loosened)
# --------------------------------------------------------------------------- #


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), dict) else v
    return out


def with_strategy_overrides(cfg: BotConfig, overrides: Mapping[str, Mapping[str, Any]]) -> BotConfig:
    """A new, fully re-validated config with per-strategy overrides applied.

    Only ``OVERRIDABLE_STRATEGY_KEYS`` may change (sizing: only the
    reduce-only ``size_multiplier``). Capital, risk limits and ML settings are
    refused, and the result still goes through every hard-limit validator."""
    data = {sid: s.model_dump() for sid, s in cfg.strategies.strategies.items()}
    for sid, ov in overrides.items():
        if sid not in data:
            raise BacktestConfigError(f"unknown strategy {sid!r}")
        bad = set(ov) - OVERRIDABLE_STRATEGY_KEYS
        if bad:
            raise BacktestConfigError(f"{sid}: overriding {sorted(bad)} is not allowed in a backtest")
        sizing = ov.get("sizing")
        if isinstance(sizing, Mapping) and set(sizing) - OVERRIDABLE_SIZING_KEYS:
            raise BacktestConfigError(f"{sid}: only sizing.size_multiplier may be overridden")
        data[sid] = _deep_merge(data[sid], ov)
    try:
        strategies = StrategyDefaults.model_validate({"strategies": data})
        return BotConfig(project_root=cfg.project_root, settings=cfg.settings, risk=cfg.risk,
                         strategies=strategies, env=cfg.env)
    except ValueError as exc:
        raise BacktestConfigError(f"invalid strategy overrides: {exc}") from exc


def with_rl_enabled(cfg: BotConfig, strategy_ids: Sequence[str]) -> BotConfig:
    """Turn ON the RL trade manager for these strategies (only the
    ``rl.enabled`` flag; action bounds stay as configured), fully re-validated."""
    unknown = set(strategy_ids) - set(cfg.strategies.strategies)
    if unknown:
        raise BacktestConfigError(f"unknown strategies {sorted(unknown)}")
    data = {sid: s.model_dump() for sid, s in cfg.strategies.strategies.items()}
    for sid in strategy_ids:
        data[sid]["rl"]["enabled"] = True
    strategies = StrategyDefaults.model_validate({"strategies": data})
    return BotConfig(project_root=cfg.project_root, settings=cfg.settings, risk=cfg.risk, strategies=strategies,
                     env=cfg.env)


def with_ml_enabled(cfg: BotConfig, strategy_ids: Sequence[str]) -> BotConfig:
    """Turn ON the ML signal filter for these strategies (only the
    ``ml.enabled`` flag; thresholds stay as configured), fully re-validated.
    The filter can only veto or shrink entries."""
    unknown = set(strategy_ids) - set(cfg.strategies.strategies)
    if unknown:
        raise BacktestConfigError(f"unknown strategies {sorted(unknown)}")
    data = {sid: s.model_dump() for sid, s in cfg.strategies.strategies.items()}
    for sid in strategy_ids:
        data[sid]["ml"]["enabled"] = True
    strategies = StrategyDefaults.model_validate({"strategies": data})
    return BotConfig(project_root=cfg.project_root, settings=cfg.settings, risk=cfg.risk, strategies=strategies,
                     env=cfg.env)


def with_settings_overrides(cfg: BotConfig, overrides: Mapping[str, Mapping[str, Any]]) -> BotConfig:
    """Override ``settings.yaml`` sections (e.g. ``paper_broker.slippage_ticks``,
    ``costs``, ``market_session.holidays``) with full re-validation."""
    allowed = {"paper_broker", "costs", "market_session", "execution"}
    bad = set(overrides) - allowed
    if bad:
        raise BacktestConfigError(f"settings sections {sorted(bad)} cannot be overridden in a backtest")
    try:
        settings = AppSettings.model_validate(_deep_merge(cfg.settings.model_dump(), overrides))
        return BotConfig(project_root=cfg.project_root, settings=settings, risk=cfg.risk,
                         strategies=cfg.strategies, env=cfg.env)
    except ValueError as exc:
        raise BacktestConfigError(f"invalid settings overrides: {exc}") from exc


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class Observations:
    """Risk-compliance and pipeline observations collected while the backtest runs."""

    max_open_trades: dict[str, int] = field(default_factory=dict)
    max_open_trades_total: int = 0
    open_after_close: dict[str, int] = field(default_factory=dict)  # day -> open trades after 15:30
    kill_switch_days: list[str] = field(default_factory=list)  # days the global kill switch was active at close
    signals: Counter[str] = field(default_factory=Counter)  # per strategy
    approved: Counter[str] = field(default_factory=Counter)  # per strategy
    rejections: dict[str, Counter[str]] = field(default_factory=dict)  # strategy -> reason -> count
    # recorded after each day's start-of-day job (day -> ...)
    futures_by_day: dict[str, str | None] = field(default_factory=dict)  # current-month future
    signal_instruments: dict[str, dict[str, str]] = field(default_factory=dict)  # strategy -> instrument key
    strategy_status: dict[str, dict[str, str]] = field(default_factory=dict)  # strategy -> status
    blocked_at_open: dict[str, dict[str, str | None]] = field(default_factory=dict)  # strategy -> kill reason

    def on_decision(self, payload: Mapping[str, Any]) -> None:
        sid = str(payload["strategy_id"])
        self.signals[sid] += 1
        if payload["approved"]:
            self.approved[sid] += 1
            return
        bucket = self.rejections.setdefault(sid, Counter())
        for reason in payload.get("reasons") or ("unspecified",):
            bucket[_reason_key(str(reason))] += 1


def _reason_key(reason: str) -> str:
    """Group rejection reasons that differ only in numbers (e.g. premiums)."""
    return re.sub(r"\d+(\.\d+)?", "#", reason)[:120]


@dataclass
class BacktestResult:
    label: str
    cfg: BotConfig
    days: list[date]
    data_source: str
    data_description: str
    strategies: list[str]
    capital: dict[str, float]
    trades: list[TradeRecord]
    orders: list[OrderRecord]
    fills: list[FillRecord]
    executions: list[Execution]
    risk_events: list[RiskEventRecord]
    daily_pnl: list[DailyPnlRecord]
    observations: Observations
    cost_model: CostModel
    elapsed_seconds: float
    warnings: list[str] = field(default_factory=list)

    @property
    def total_capital(self) -> float:
        return float(self.cfg.risk.capital.total_capital)

    def closed_trades(self, strategy_id: str | None = None) -> list[TradeRecord]:
        return [t for t in self.trades if t.status.value == "CLOSED"
                and (strategy_id is None or t.strategy_id == strategy_id)]


@dataclass(frozen=True)
class BacktestOptions:
    label: str = "backtest"
    spread: SpreadModel = field(default_factory=SpreadModel)
    strategies: tuple[str, ...] | None = None  # None = every strategy enabled in config
    database_path: Path | None = None  # None = in memory
    progress: Callable[[date, int, int], None] | None = None
    #: Phase 7: run these strategies with the ML signal filter ON, using
    #: ``ml_models`` (per strategy). ``ml_research`` allows SYNTHETIC research
    #: models (backtests only). Empty = the configured ``ml.enabled`` flags.
    ml_strategies: tuple[str, ...] = ()
    ml_models: Mapping[str, StrategyModels] | None = None
    ml_research: bool = False
    #: Phase 8: run these strategies with the RL trade manager ON, using
    #: ``rl_models`` (per strategy). ``rl_research`` allows SYNTHETIC research
    #: models (backtests only). Empty = the configured ``rl.enabled`` flags.
    rl_strategies: tuple[str, ...] = ()
    rl_models: Mapping[str, LoadedModel] | None = None
    rl_research: bool = False


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #


class Backtester:
    def __init__(self, cfg: BotConfig, dataset: HistoricalDataset, options: BacktestOptions | None = None) -> None:
        self.options = options or BacktestOptions()
        if cfg.mode is not TradingMode.PAPER:
            raise BacktestConfigError("backtests run in PAPER mode only")
        if self.options.strategies is not None:
            unknown = set(self.options.strategies) - set(cfg.strategies.strategies)
            if unknown:
                raise BacktestConfigError(f"unknown strategies {sorted(unknown)}")
            cfg = with_strategy_overrides(cfg, {sid: {"enabled": sid in self.options.strategies}
                                                for sid in cfg.strategies.strategies})
        if self.options.ml_strategies:
            cfg = with_ml_enabled(cfg, self.options.ml_strategies)
        if self.options.rl_strategies:
            cfg = with_rl_enabled(cfg, self.options.rl_strategies)
        self.cfg = cfg
        self.dataset = dataset
        session = cfg.settings.market_session
        index_key = cfg.settings.instruments.nifty_index_key
        dataset.validate(session_open=session.market_open, session_close=session.market_close,
                         required_keys=(index_key,))
        self.days = dataset.days()
        for d in self.days:
            if not session.holidays_known_for(d.year):
                raise BacktestDataError(f"no NSE holidays configured for {d.year}: the risk gate would block every "
                                        "entry; add that year's holidays to market_session.holidays")
            if not session.is_trading_day(d):
                raise BacktestDataError(f"{d} is not an NSE trading day (weekend or configured holiday)")
        self.cost = CostModel(cfg.settings.costs, slippage_ticks=cfg.settings.paper_broker.slippage_ticks,
                              spread=self.options.spread)

    def _database(self) -> Database:
        path = self.options.database_path
        if path is None:
            return Database(":memory:")
        path = Path(path)
        if path.resolve() == self.cfg.database_path.resolve():
            raise BacktestConfigError("refusing to backtest into the trading database")
        if path.exists():
            raise BacktestConfigError(f"{path} already exists; backtests write to a fresh database")
        return Database(path)

    async def run(self) -> BacktestResult:
        started = wall_time.perf_counter()
        cfg, ds = self.cfg, self.dataset
        session = cfg.settings.market_session
        first = self.days[0]
        clock = SimClock(datetime.combine(first, time(9, 0), tzinfo=IST))
        db = self._database()
        db.initialize()
        repos = Repositories(db, TradingMode.PAPER, clock=clock)
        sync_strategies(repos.strategies, cfg.strategies.strategies)
        md = MarketDataService(now_fn=clock, stale_after_seconds=cfg.settings.websocket.stale_after_seconds)
        instruments = {i.instrument_key: i for i in ds.instruments}
        broker = PaperBroker(quotes=md.get, settings=cfg.settings.paper_broker, costs=cfg.settings.costs,
                             instruments=ds.instruments, now_fn=clock)
        subscribed: set[str] = set()

        async def subscriber(add: set[str], remove: set[str]) -> None:
            subscribed.update(add)
            subscribed.difference_update(remove)

        scheduler = TradingScheduler()
        foreign = asyncio.all_tasks()  # tasks that are not the backtest's (never touched)
        # reconcile_confirm_delay=0: the startup reconciliation runs inline in prepare(), where a parked
        # confirmation wait could never be woken; a mismatch then kills at once (fail-safe).
        bot = TradingBot(cfg, repos=repos, broker=broker, market_data=md, source=HistoricalSource(ds.instruments),
                         subscriber=subscriber, now_fn=clock, sleep=clock.sleep, scheduler=scheduler,
                         reconcile_confirm_delay=0, instrument_refresh_attempts=1, wall_fn=clock,
                         ml_models=self.options.ml_models, ml_research=self.options.ml_research,
                         rl_models=self.options.rl_models, rl_research=self.options.rl_research)
        raw_fills: list[tuple[OrderUpdate, float]] = []

        def on_fill(u: OrderUpdate) -> None:
            if u.last_fill_quantity > 0 and u.last_fill_price is not None:
                tick = md.get(u.instrument_key)
                raw_fills.append((u, tick.ltp if tick is not None else u.last_fill_price))

        broker.add_order_listener(on_fill)
        obs = Observations()
        bot.bus.subscribe(Topic.ENTRY_DECISION, lambda ev: obs.on_decision(ev.payload))
        warnings: list[str] = []
        await bot.prepare(instruments=ds.instruments)
        bot.start()
        bot.schedule()
        sim = SimulatedScheduler(scheduler)
        sim.mark_daily_done("start_of_day", first)
        sids = [s.strategy_id for s in bot.strategies]
        try:
            for n, d in enumerate(self.days):
                clock.set(datetime.combine(d, time(9, 0), tzinfo=IST))
                await sim.run_due(clock.now)  # start_of_day (instrument list, new-day reset, reconciliation)
                await settle_simulation()
                self._observe_day_start(bot, d, obs)
                replay = DayReplay(ds.day(d), instruments, self.cost)
                for t in replay.steps:
                    clock.set(t)  # wakes retries/backoffs due by now; they run after this step's ticks
                    md.on_ticks(replay.ticks(t, subscribed))
                    await sim.run_due(t)
                    await settle_simulation()
                    self._observe(bot, sids, obs)
                clock.set(datetime.combine(d, session.market_close, tzinfo=IST))
                await sim.run_due(clock.now)
                await settle_simulation()
                left = bot.stack.positions.open_trade_count()
                if left:
                    obs.open_after_close[d.isoformat()] = left
                    warnings.append(f"{d}: {left} trade(s) still open after the close")
                if bot.stack.kill_switch.global_event is not None:
                    obs.kill_switch_days.append(d.isoformat())
                if self.options.progress is not None:
                    self.options.progress(d, n + 1, len(self.days))
        finally:
            cancelled = await self._stop(bot, clock, foreign)
        if cancelled:
            warnings.append(f"{cancelled} background task(s) still waiting at the end of the run were cancelled")
        result = self._collect(bot, repos, raw_fills, obs, warnings, wall_time.perf_counter() - started)
        db.close()
        return result

    @staticmethod
    async def _stop(bot: TradingBot, clock: SimClock, foreign: set[asyncio.Task[Any]]) -> int:
        """End of the run: cancel work still parked on simulated time (it has
        no future to run in), then stop the bot."""
        me = asyncio.current_task()
        leftover = [t for t in asyncio.all_tasks() if t not in foreign and t is not me and not t.done()]
        for t in leftover:
            t.cancel()
        await asyncio.gather(*leftover, return_exceptions=True)
        clock.close()
        bot.stop()
        return len(leftover)

    @staticmethod
    def _observe_day_start(bot: TradingBot, d: date, obs: Observations) -> None:
        day = d.isoformat()
        obs.futures_by_day[day] = bot.futures.instrument_key if bot.futures is not None else None
        obs.signal_instruments[day] = {s.strategy_id: s.signal_instrument_key for s in bot.strategies}
        obs.strategy_status[day] = {r.strategy_id: r.status.value for r in bot.repos.strategies.list_all()}
        obs.blocked_at_open[day] = {s.strategy_id: bot.stack.kill_switch.block_reason(s.strategy_id)
                                    for s in bot.strategies}

    @staticmethod
    def _observe(bot: TradingBot, sids: Sequence[str], obs: Observations) -> None:
        pm = bot.stack.positions
        total = 0
        for sid in sids:
            n = pm.open_trade_count(sid)
            total += n
            if n > obs.max_open_trades.get(sid, 0):
                obs.max_open_trades[sid] = n
        obs.max_open_trades_total = max(obs.max_open_trades_total, total)

    def _collect(self, bot: TradingBot, repos: Repositories, raw_fills: list[tuple[OrderUpdate, float]],
                 obs: Observations, warnings: list[str], elapsed: float) -> BacktestResult:
        orders = repos.orders.list_all()
        by_coid = {o.client_order_id: o for o in orders}
        executions = []
        for u, mid in raw_fills:
            o = by_coid.get(u.client_order_id or "")
            if o is None or u.last_fill_price is None:
                continue
            executions.append(Execution(
                strategy_id=o.strategy_id, client_order_id=o.client_order_id, side=OrderSide(o.side),
                quantity=u.last_fill_quantity, price=u.last_fill_price, mid=mid, charges=u.last_fill_charges,
                slippage_per_unit=u.last_fill_slippage, first_fill_of_order=u.filled_quantity == u.last_fill_quantity))
        sids = sorted(self.cfg.strategies.strategies)
        enabled = [sid for sid in sids if self.cfg.strategies.strategies[sid].enabled]
        trades = [t for sid in sids for t in repos.trades.list_for_strategy(sid)]
        daily = [r for d in self.days for r in repos.daily_pnl.list_for_day(d)]
        return BacktestResult(
            label=self.options.label, cfg=self.cfg, days=list(self.days), data_source=self.dataset.source,
            data_description=self.dataset.description, strategies=enabled,
            capital={sid: float(self.cfg.strategies.strategies[sid].allocated_capital) for sid in enabled},
            trades=trades, orders=orders, fills=repos.fills.list_all(), executions=executions,
            risk_events=list(reversed(repos.risk_events.recent(limit=1_000_000))), daily_pnl=daily,
            observations=obs, cost_model=self.cost, elapsed_seconds=round(elapsed, 2), warnings=warnings)


def run_backtest(cfg: BotConfig, dataset: HistoricalDataset, options: BacktestOptions | None = None) -> BacktestResult:
    """Synchronous convenience wrapper."""
    return asyncio.run(Backtester(cfg, dataset, options).run())
