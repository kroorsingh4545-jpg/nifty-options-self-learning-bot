"""Helpers for PAPER-launcher tests: a fake market feed and a config bound to
a temporary database. Market data here is SYNTHETIC (tests only); nothing
connects to Upstox."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from broker.broker_interface import MarketDataSource
from config.loader import load_config
from config.settings import BotConfig, DatabaseSection
from core.enums import AutonomyLevel, EvidenceSource, TradingMode
from data.market_data import MarketDataService
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.repositories import Repositories
from learning.deployment_controller import DeploymentController
from learning.mutation_engine import MutationEngine
from learning.promotion_gate import Outcome, PromotionGate
from learning.validation_engine import GATES, GateResult, ValidationReport
from runtime.paper import MarketConnections, PaperSession, RiskHooks, SessionOptions
from tests.fakes import FakeWallClock
from tests.synthetic import INDEX, index_path, tick
from tests.unit.test_bot_integration import NoSource

SID = "orb_options"


def paper_cfg(tmp_path: Path, *, autonomy: AutonomyLevel | None = None, **env: Any) -> BotConfig:
    """The repository config with fake Upstox credentials and a temp database."""
    cfg = load_config(use_default_env_file=False)
    env_s = cfg.env.model_copy(update={"upstox_api_key": SecretStr("test-key"),
                                       "upstox_access_token": SecretStr("test-token"), **env})
    settings = cfg.settings.model_copy(update={"database": DatabaseSection(path=tmp_path / "trading.db")})
    if autonomy is not None:
        settings = settings.model_copy(update={"app": settings.app.model_copy(update={"autonomy_level": autonomy})})
    return cfg.model_copy(update={"env": env_s, "settings": settings})


class FakeFeed:
    """Stands in for the Upstox market-data WebSocket."""

    def __init__(self) -> None:
        self.subscribed: set[str] = set()
        self.started = False
        self.stopped = False
        self.listeners: list[Callable[[str, Any, Any], None]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def subscribe(self, instrument_keys: Iterable[str]) -> None:
        self.subscribed |= set(instrument_keys)

    async def unsubscribe(self, instrument_keys: Iterable[str]) -> None:
        self.subscribed -= set(instrument_keys)

    def add_status_listener(self, fn: Callable[[str, Any, Any], None]) -> None:
        self.listeners.append(fn)

    def emit(self, old: str, new: str) -> None:
        for fn in self.listeners:
            fn("market_data", old, new)

    def health(self) -> dict[str, Any]:
        return {"market_data": {"state": "CONNECTED", "connected": True, "subscriptions": len(self.subscribed)}}


class FakeConnections:
    """ConnectionFactory with SYNTHETIC market data driven by the test."""

    def __init__(self, wall: FakeWallClock, source: MarketDataSource | None = None) -> None:
        self.wall = wall
        self.feed = FakeFeed()
        self.source = source or NoSource()
        self.hooks: RiskHooks | None = None
        self.md: MarketDataService | None = None
        self.closed = False
        self.clock_probe: Any = None  # None = no clock reference (the session skips the clock monitor)

    def __call__(self, cfg: BotConfig, hooks: RiskHooks) -> MarketConnections:
        self.hooks = hooks
        self.md = MarketDataService(now_fn=self.wall)

        async def aclose() -> None:
            self.closed = True

        return MarketConnections(self.md, self.source, self.feed,
                                 lambda: {"upstox": "fake", "upstox_connected": True, "websocket": "CONNECTED"},
                                 lambda: {"standard": {"per_second": 0}}, aclose, evidence=EvidenceSource.SYNTHETIC,
                                 clock_probe=self.clock_probe)


def session(cfg: BotConfig, wall: FakeWallClock, conns: FakeConnections, **opts: Any) -> PaperSession:
    return PaperSession(cfg, options=SessionOptions(**opts), connections=conns, now_fn=wall,
                        install_signal_handlers=False, run_scheduler=False)


async def drive(s: PaperSession, conns: FakeConnections, points: list[tuple[datetime, float]],
                premium: float = 100.0) -> None:
    """Replay a SYNTHETIC index path (options first so quotes are fresh)."""
    bot, md = s.bot, conns.md
    assert bot is not None and md is not None and bot.universe is not None
    first = True
    for t, spot in index_path(points):
        conns.wall.now = t
        if md.ltp(INDEX) is not None:
            for key in bot.universe.wanted(spot) | bot.universe.subscribed:
                md.on_tick(tick(key, premium, t, spread=0.1, vtt=250_000, oi=1_200_000))
        md.on_tick(tick(INDEX, spot, t, spread=0.0))
        await bot.settle()
        if first:
            await bot.recenter_universe()
            first = False


def fabricated(cand: Any) -> ValidationReport:
    """A FABRICATED passing report labelled HISTORICAL, only to exercise the
    deployment path (no market data behind it)."""
    gates = [GateResult(g, True, "fabricated for the launcher test") for g in GATES]
    return ValidationReport(cand.strategy_id, cand.base_version, cand.version, EvidenceSource.HISTORICAL, gates,
                            {"candidate_oos": {"expectancy": 200.0}})


def deploy_version(cfg: BotConfig, change: dict[str, Any], sid: str = SID) -> Any:
    """Put a PAPER_ACTIVE version of ``sid`` into the trading database."""
    db = Database(cfg.database_path)
    db.initialize()
    try:
        repos = Repositories(db, TradingMode.PAPER)
        sync_strategies(repos.strategies, cfg.strategies.strategies)

        def level() -> AutonomyLevel:
            return AutonomyLevel.PAPER_AUTO_IMPROVE

        gate = PromotionGate(repos, cfg, level)
        parent = gate.register_baseline(cfg, sid)
        cand = MutationEngine(cfg).propose(sid, change, taken_versions=repos.strategy_versions.versions(sid))
        rec = gate.register_candidate(cand, None, parent)
        assert gate.decide(rec.version_uid, fabricated(cand), cfg).outcome is Outcome.PAPER_READY
        DeploymentController(repos, cfg, level).deploy_paper(rec.version_uid)
        return rec
    finally:
        db.close()
