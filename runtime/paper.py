"""PAPER launcher: the composition root for a real-time PAPER session.

Real NIFTY market data from Upstox (read-only REST + the market-data
WebSocket) drives the bot; every order goes to ``PaperBroker`` and is
simulated. Nothing here can place a real order: the broker factory only
builds ``PaperBroker`` in PAPER mode, ``UpstoxRestClient``'s order methods
always refuse, no portfolio stream is opened, and the session refuses to
start unless ``TRADING_MODE=PAPER`` and LIVE routing is absent from the build.

Start-up (``PaperSession.start``):

1. pre-flight checks (``preflight``); any fatal failure stops the launch
2. open and migrate the trading database (the bot is the only migrator)
3. resolve the strategy versions to trade: the YAML config with every PAPER
   deployment from the learning loop overlaid
   (``DeploymentController.resolve``). This is the only place a deployment
   takes effect, so a version deployed during a session runs from the next
   start. ``--baseline`` or an autonomy level that allows no learned versions
   (OBSERVE_ONLY) trades the YAML config; deployment records are untouched.
4. compose: rate limiter, REST client, market data, WebSocket feed,
   PaperBroker, TradingBot. Feed state, Upstox auth failures and rate-limit
   lock-outs are wired into the kill switch.
5. ``prepare`` (instrument list, restore, reconciliation), schedule the jobs,
   connect the feed, start the engine (unless ``paused``)

``run`` then waits for Ctrl+C / SIGTERM / ``stop_at`` and shuts down in order:
no new entries, settle background work, journal pending trades, publish the
final state, close the feed and clients, close the database. Open PAPER
trades stay in the database and are restored at the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Protocol

from broker.broker_interface import MarketDataSource
from broker.factory import create_broker
from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config import hard_limits
from config.settings import BotConfig
from core.autonomy import may_auto_deploy_paper
from core.clock import IST, now_ist
from core.enums import AutonomyLevel, EvidenceSource, TradingMode
from data.market_data import MarketDataService
from data.storage.bootstrap import sync_strategies
from data.storage.database import SCHEMA_VERSION, Database
from data.storage.repositories import Repositories
from data.websocket_manager import WebSocketManager
from learning.autonomy_source import persisted_autonomy
from learning.deployment_controller import DeploymentController, VersionInUse
from monitoring.alerts import AlertSink
from monitoring.json_logger import get_logger
from runtime.bot import TradingBot
from runtime.learning_loop import LearningLoopRunner

log = get_logger(__name__)


class PaperLaunchError(RuntimeError):
    """The PAPER session must not start."""


# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True

    @property
    def label(self) -> str:
        return "PASS" if self.ok else ("FAIL" if self.fatal else "WARN")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def preflight(cfg: BotConfig, *, now: datetime | None = None, need_upstox: bool = True) -> list[Check]:
    """Offline checks (no network). Fatal failures block the launch."""
    now = (now or now_ist()).astimezone(IST)
    session = cfg.settings.market_session
    risk = cfg.risk
    out = [
        Check("PAPER mode", cfg.mode is TradingMode.PAPER, f"TRADING_MODE={cfg.mode.value}"),
        Check("No LIVE order routing", not hard_limits.LIVE_TRADING_AVAILABLE,
              "this build cannot place real orders (PaperBroker only)" if not hard_limits.LIVE_TRADING_AVAILABLE
              else "LIVE routing is present: this launcher refuses to run"),
        Check("Upstox credentials", cfg.env.upstox_credentials_present,
              "UPSTOX_API_KEY and UPSTOX_ACCESS_TOKEN set (used for read-only market data; the token expires "
              "daily at " + cfg.settings.broker.token_expiry_time.strftime("%H:%M") + " IST)"
              if cfg.env.upstox_credentials_present else
              "set UPSTOX_API_KEY and UPSTOX_ACCESS_TOKEN in .env (see README: token generation)",
              fatal=need_upstox),
        Check("NSE holiday calendar", session.holidays_known_for(now.year),
              f"holidays configured for {now.year}" if session.holidays_known_for(now.year) else
              f"no holidays for {now.year} in market_session.holidays: every new entry is blocked", fatal=False),
        Check("Trading day", session.is_trading_day(now.date()),
              f"{now.date()} is an NSE trading day" if session.is_trading_day(now.date()) else
              f"{now.date()} is a weekend or holiday: the bot idles until the next trading day", fatal=False),
    ]
    db_path = cfg.database_path
    research = [cfg.resolve_path(cfg.settings.learning.research_dir), cfg.resolve_path(cfg.settings.ml.research_dir)]
    if any(_inside(db_path, r) for r in research):
        out.append(Check("Trading database", False, f"{db_path} is inside a research directory"))
    elif db_path.is_file():
        found = Database(db_path)
        try:
            version = found.current_schema_version()
        finally:
            found.close()
        if version is None or version > SCHEMA_VERSION:
            out.append(Check("Trading database", False, f"{db_path} has schema v{version}, this code expects "
                                                        f"v{SCHEMA_VERSION}: refusing to start"))
        else:
            migrate = ", will be migrated" if version < SCHEMA_VERSION else ""
            out.append(Check("Trading database", True, f"{db_path} (schema v{version}{migrate})"))
    else:
        out.append(Check("Trading database", True, f"{db_path} (will be created)"))
    enabled = [sid for sid, s in cfg.strategies.strategies.items() if s.enabled]
    out.append(Check("Enabled strategies", bool(enabled), ", ".join(enabled) or "none enabled: nothing will trade",
                     fatal=False))
    tr = risk.time_rules
    out.append(Check("Risk limits", True,
                     f"per trade Rs {risk.per_trade.max_risk_per_trade:,.0f}, per strategy/day "
                     f"Rs {risk.per_strategy.max_daily_loss:,.0f}, global/day Rs {risk.global_.max_daily_loss:,.0f}, "
                     f"max open {risk.global_.max_open_trades}, no entries after {tr.no_new_entries_after:%H:%M}, "
                     f"square-off {tr.square_off_start:%H:%M}-{tr.square_off_end:%H:%M}; kill switch armed"))
    out.append(Check("Dashboard login", bool(cfg.env.dashboard_username),
                     "DASHBOARD_USERNAME / DASHBOARD_PASSWORD set" if cfg.env.dashboard_username else
                     "dashboard login not configured (streamlit run dashboard/app.py refuses to start)", fatal=False))
    return out


def render_checks(checks: Iterable[Check]) -> str:
    return "\n".join(f"[{c.label}] {c.name}: {c.detail}" for c in checks)


# --------------------------------------------------------------------------- #
# Which strategy versions this session trades
# --------------------------------------------------------------------------- #


def resolve_startup_config(cfg: BotConfig, repos: Repositories, *, autonomy: AutonomyLevel,
                           baseline: bool = False) -> tuple[BotConfig, dict[str, VersionInUse]]:
    """The config this PAPER session trades: the YAML config with every PAPER
    deployment overlaid (each one re-validated), unless ``baseline`` is set or
    the autonomy level allows no learned version (OBSERVE_ONLY)."""
    controller = DeploymentController(repos, cfg, autonomy_fn=lambda: autonomy)
    if baseline or not may_auto_deploy_paper(autonomy):
        why = "--baseline" if baseline else f"autonomy {AutonomyLevel(autonomy).value} runs the YAML baseline"
        out = {}
        for sid, scfg in cfg.strategies.strategies.items():
            active = controller.active(sid)
            note = f"{why}; deployed {active.version} not applied" if active is not None else why
            out[sid] = VersionInUse(sid, scfg.version, "YAML", None, note)
        return cfg, out
    return controller.resolve()


def render_versions(versions: dict[str, VersionInUse]) -> str:
    return "\n".join(f"  {v.strategy_id}: {v.version} [{v.source}] {v.note}" for v in versions.values())


# --------------------------------------------------------------------------- #
# Market connections (Upstox read-only by default; fakes in tests)
# --------------------------------------------------------------------------- #


class Feed(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def subscribe(self, instrument_keys: Iterable[str]) -> None: ...

    async def unsubscribe(self, instrument_keys: Iterable[str]) -> None: ...

    def add_status_listener(self, fn: Callable[[str, Any, Any], None]) -> None: ...

    def health(self) -> dict[str, Any]: ...


@dataclass
class RiskHooks:
    """Late-bound to the bot's risk manager (the clients exist before the bot)."""

    on_auth_failure: Callable[[str], None]
    on_circuit_trip: Callable[[Any], None]


@dataclass
class MarketConnections:
    market_data: MarketDataService
    source: MarketDataSource
    feed: Feed
    status: Callable[[], dict[str, Any]]
    rate_limits: Callable[[], dict[str, Any]] | None = None
    aclose: Callable[[], Awaitable[None]] | None = None
    #: what this feed is, for the trade journal (only LIVE_FEED / HISTORICAL count as evidence)
    evidence: EvidenceSource = EvidenceSource.LIVE_FEED


ConnectionFactory = Callable[[BotConfig, RiskHooks], MarketConnections]


def upstox_connections(cfg: BotConfig, hooks: RiskHooks) -> MarketConnections:
    """Read-only Upstox clients: rate-limited REST (instrument list, feed
    authorisation, LTP recovery) and ONE market-data WebSocket. No portfolio
    stream: PAPER orders never exist at Upstox."""
    limiter = UpstoxRateLimiter.from_config(cfg, on_circuit_trip=hooks.on_circuit_trip)
    rest = UpstoxRestClient.from_config(cfg, limiter, on_auth_failure=hooks.on_auth_failure)
    md = MarketDataService(stale_after_seconds=cfg.settings.websocket.stale_after_seconds, rest=rest,
                           snapshot_debounce_seconds=cfg.settings.broker.debounce_seconds)
    feed = WebSocketManager(authorizer=rest, market_data=md, settings=cfg.settings.websocket, enable_portfolio=False)

    def status() -> dict[str, Any]:
        h = feed.health().get("market_data", {})
        auth = rest.auth_failed
        connected = bool(h.get("connected")) and auth is None
        return {"upstox": f"auth failed: {auth}" if auth else "read-only market data (no order routing)",
                "upstox_connected": connected,
                "websocket": f"{h.get('state', 'IDLE')} ({h.get('subscriptions', 0)} subscriptions)"}

    return MarketConnections(md, rest, feed, status, limiter.usage, rest.aclose)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionOptions:
    paused: bool = False  # start with the engine PAUSED (start it from the dashboard)
    baseline: bool = False  # trade the YAML config, ignoring PAPER deployments
    stop_at: time | None = None  # IST time today to end the session
    shutdown_timeout_seconds: float = 30.0


@dataclass
class SessionResult:
    reason: str
    open_trades: int
    versions: dict[str, VersionInUse] = field(default_factory=dict)


class PaperSession:
    def __init__(self, cfg: BotConfig, *, options: SessionOptions | None = None,
                 connections: ConnectionFactory = upstox_connections,
                 alert_sinks: Sequence[AlertSink] | None = None,
                 now_fn: Callable[[], datetime] = now_ist, install_signal_handlers: bool = True,
                 run_scheduler: bool = True) -> None:
        """``run_scheduler=False`` registers the jobs without starting the
        real-time scheduler (tests drive the jobs by hand)."""
        if cfg.mode is not TradingMode.PAPER:
            raise PaperLaunchError(f"TRADING_MODE is {cfg.mode.value}: only PAPER sessions can run")
        if hard_limits.LIVE_TRADING_AVAILABLE:  # pragma: no cover - this build has no LIVE routing
            raise PaperLaunchError("this launcher runs PAPER only and refuses a build with LIVE routing")
        self.yaml_cfg = cfg
        self.options = options or SessionOptions()
        self._connections = connections
        self._alert_sinks = tuple(alert_sinks or ())
        self._now = now_fn
        self._signals = install_signal_handlers
        self._run_scheduler = run_scheduler
        self.cfg: BotConfig = cfg
        self.versions: dict[str, VersionInUse] = {}
        self.autonomy: AutonomyLevel = AutonomyLevel(cfg.settings.app.autonomy_level)
        self.db: Database | None = None
        self.repos: Repositories | None = None
        self.conn: MarketConnections | None = None
        self.bot: TradingBot | None = None
        self._stop = asyncio.Event()
        self.stop_reason = ""
        self._timers: list[asyncio.TimerHandle] = []
        self._closed = False
        self.learning_loop: LearningLoopRunner | None = None

    # Hooks into the bot's risk manager --------------------------------------
    def _on_auth_failure(self, reason: str) -> None:
        if self.bot is not None:
            self.bot.stack.risk.on_auth_failure(reason)
        else:
            log.critical("Upstox authentication failed before the bot was ready", extra={"reason": reason})

    def _on_circuit_trip(self, category: Any) -> None:
        if self.bot is not None:
            self.bot.stack.risk.on_rate_limit_trip(category)
        else:
            log.critical("Upstox rate-limit circuit locked before the bot was ready",
                         extra={"category": str(category)})

    async def _subscribe(self, add: set[str], remove: set[str]) -> None:
        assert self.conn is not None
        if remove:
            await self.conn.feed.unsubscribe(remove)
        if add:
            await self.conn.feed.subscribe(add)

    def _alert_channels(self) -> dict[str, Any]:
        out: dict[str, Any] = {"dashboard": "on", "log": "on"}
        for s in self._alert_sinks:
            describe = getattr(s, "describe", None)
            out[getattr(s, "channel", type(s).__name__)] = describe() if callable(describe) else "on"
        return out

    # Start-up ---------------------------------------------------------------
    def open_database(self) -> Repositories:
        db = Database(self.yaml_cfg.database_path)
        db.initialize()
        self.db = db
        self.repos = Repositories(db, TradingMode.PAPER)
        return self.repos

    async def start(self) -> TradingBot:
        fatal = [c for c in preflight(self.yaml_cfg, now=self._now()) if not c.ok and c.fatal]
        if fatal:
            raise PaperLaunchError("pre-flight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in fatal))
        repos = self.open_database()
        # YAML capped by the last dashboard change (a switch to OBSERVE_ONLY survives restarts).
        self.autonomy = persisted_autonomy(repos, self.yaml_cfg)
        self.cfg, self.versions = resolve_startup_config(self.yaml_cfg, repos, autonomy=self.autonomy,
                                                         baseline=self.options.baseline)
        sync_strategies(repos.strategies, self.cfg.strategies.strategies)
        log.warning("PAPER session strategy versions", extra={
            "autonomy": self.autonomy.value,
            "versions": {sid: f"{v.version} [{v.source}]" for sid, v in self.versions.items()}})
        self.conn = conn = self._connections(self.cfg, RiskHooks(self._on_auth_failure, self._on_circuit_trip))
        broker = create_broker(self.cfg, quotes=conn.market_data.get, now_fn=self._now)
        bot = TradingBot(self.cfg, repos=repos, broker=broker, market_data=conn.market_data, source=conn.source,
                         subscriber=self._subscribe, now_fn=self._now, alert_sinks=self._alert_sinks,
                         evidence_source=conn.evidence,
                         version_sources={sid: v.version_uid or "YAML" for sid, v in self.versions.items()})
        bot.mode.set_autonomy(self.autonomy)
        self.bot = bot
        conn.feed.add_status_listener(bot.stack.risk.on_stream_state)
        bot.control.set_status_providers(connection_status=conn.status, rate_limit_usage=conn.rate_limits,
                                         alert_channels=self._alert_channels)
        for sink in self._alert_sinks:
            starter = getattr(sink, "start", None)
            if starter is not None:
                await starter()
        await bot.prepare()
        bot.schedule()
        loop_cfg = self.yaml_cfg.settings.learning.loop
        if loop_cfg.enabled:
            self.learning_loop = LearningLoopRunner(bot, self.yaml_cfg)
            bot.scheduler.daily("learning_loop", loop_cfg.run_at, self.learning_loop.run_once)
            log.warning("learning loop scheduled", extra={"run_at": loop_cfg.run_at.strftime("%H:%M"),
                                                          "verify_data": str(loop_cfg.verify_data)})
        await conn.feed.start()
        if self._run_scheduler:
            bot.scheduler.start()
        if self.options.paused:
            bot.pause()
            log.warning("PAPER engine PAUSED at start-up: start it from the dashboard")
        else:
            bot.start()
        await bot.control.publish_state()
        log.warning("PAPER session started", extra={"mode": "PAPER", "paused": self.options.paused,
                                                    "strategies": [s.strategy_id for s in bot.strategies]})
        return bot

    # Run / stop -------------------------------------------------------------
    def request_stop(self, reason: str) -> None:
        if not self._stop.is_set():
            self.stop_reason = reason
            log.warning("PAPER session stop requested", extra={"reason": reason})
            self._stop.set()

    def _install_stop_triggers(self) -> None:
        loop = asyncio.get_running_loop()
        if self._signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")
        if self.options.stop_at is not None:
            now = self._now().astimezone(IST)
            at = datetime.combine(now.date(), self.options.stop_at, tzinfo=IST)
            delay = (at - now).total_seconds()
            if delay <= 0:
                raise PaperLaunchError(f"--stop-at {self.options.stop_at:%H:%M} has already passed today")
            self._timers.append(loop.call_later(delay, self.request_stop, f"stop-at {self.options.stop_at:%H:%M}"))

    async def run(self) -> SessionResult:
        """Start, wait for a stop request (or Ctrl+C), then shut down."""
        try:
            self._install_stop_triggers()
            await self.start()
            await self._stop.wait()
        except asyncio.CancelledError:
            self.stop_reason = self.stop_reason or "interrupted"
        finally:
            await self.shutdown()
        bot = self.bot
        return SessionResult(self.stop_reason, bot.stack.positions.open_trade_count() if bot else 0, self.versions)

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for t in self._timers:
            t.cancel()
        bot = self.bot
        timeout = self.options.shutdown_timeout_seconds
        if bot is not None:
            bot.stop()  # no new entries; scheduler stopped
            try:
                await asyncio.wait_for(bot.settle(), timeout)
            except Exception:
                log.exception("PAPER shutdown: background work did not settle")
            if bot.learning is not None:
                bot.learning.flush()
            open_trades = bot.stack.positions.open_trades()
            if open_trades:
                log.warning("PAPER session stopped with open trades: they are restored at the next start, and "
                            "their stops are monitored only while the bot runs",
                            extra={"trades": [t.trade_uid for t in open_trades]})
            with contextlib.suppress(Exception):
                await bot.control.publish_state()
        if self.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.feed.stop(), timeout)
            if self.conn.aclose is not None:
                with contextlib.suppress(Exception):
                    await self.conn.aclose()
        for sink in self._alert_sinks:
            closer = getattr(sink, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer(), timeout)
        if self.db is not None:
            self.db.close()
        log.warning("PAPER session stopped", extra={"reason": self.stop_reason or "shutdown"})


__all__ = ["Check", "ConnectionFactory", "Feed", "MarketConnections", "PaperLaunchError", "PaperSession",
           "RiskHooks", "SessionOptions", "SessionResult", "preflight", "render_checks", "render_versions",
           "resolve_startup_config", "upstox_connections"]
