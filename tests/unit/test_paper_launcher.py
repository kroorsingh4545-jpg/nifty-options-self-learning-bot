"""PAPER launcher (Phase 10): pre-flight checks, applying PAPER deployments at
start-up, kill-switch wiring of the feed and Upstox auth, orderly shutdown.

Market data is SYNTHETIC (tests only); no test connects to Upstox. The
deployed versions come from FABRICATED validation reports.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from backtest.backtester import with_strategy_overrides
from broker.paper_broker import PaperBroker
from config.settings import BotConfig
from core.enums import AutonomyLevel, EvidenceSource, OptionType, TradeStatus, TradingMode
from data.storage.database import SCHEMA_VERSION, Database
from data.storage.repositories import Repositories
from risk.kill_switch import KillReason
from runtime import __main__ as cli
from runtime.paper import (
    PaperLaunchError,
    PaperSession,
    RiskHooks,
    preflight,
    resolve_startup_config,
    upstox_connections,
)
from tests.fakes import FakeWallClock
from tests.paper_support import SID, FakeConnections, deploy_version, drive, paper_cfg, session
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY

A = AutonomyLevel


def checks_by_name(cfg: BotConfig, **kw: object) -> dict[str, object]:
    return {c.name: c for c in preflight(cfg, now=at(9, 14), **kw)}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #


def test_preflight_passes_for_a_paper_config_with_credentials(tmp_path: Path) -> None:
    checks = preflight(paper_cfg(tmp_path), now=at(9, 14))
    fatal = [c for c in checks if not c.ok and c.fatal]
    assert not fatal, fatal
    names = {c.name for c in checks}
    assert {"PAPER mode", "No LIVE order routing", "Upstox credentials", "Trading database", "Risk limits"} <= names


def test_preflight_fails_without_upstox_credentials(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    cfg = cfg.model_copy(update={"env": cfg.env.model_copy(update={"upstox_access_token": None})})
    c = checks_by_name(cfg)["Upstox credentials"]
    assert not c.ok and c.fatal  # type: ignore[attr-defined]


def test_preflight_fails_in_live_mode_and_the_session_refuses(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    live = cfg.model_copy(update={"env": cfg.env.model_copy(update={"trading_mode": TradingMode.LIVE})})
    c = checks_by_name(live)["PAPER mode"]
    assert not c.ok and c.fatal  # type: ignore[attr-defined]
    with pytest.raises(PaperLaunchError, match="only PAPER"):
        PaperSession(live)


def test_preflight_refuses_a_newer_schema_or_a_research_database(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    db = Database(cfg.database_path)
    db.initialize()
    db.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, '2026-11-06T00:00:00+05:30')",
               (SCHEMA_VERSION + 1,))
    db.close()
    c = checks_by_name(cfg)["Trading database"]
    assert not c.ok and "refusing" in c.detail  # type: ignore[attr-defined]

    research = cfg.resolve_path(cfg.settings.learning.research_dir) / "x" / "learning.db"
    inside = cfg.model_copy(update={"settings": cfg.settings.model_copy(update={
        "database": cfg.settings.database.model_copy(update={"path": research})})})
    c2 = checks_by_name(inside)["Trading database"]
    assert not c2.ok and "research" in c2.detail  # type: ignore[attr-defined]


def test_missing_holidays_and_non_trading_days_only_warn(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    by = {c.name: c for c in preflight(cfg, now=at(9, 14, day=(2027, 1, 2)))}  # a Saturday in an unlisted year
    for name in ("NSE holiday calendar", "Trading day"):
        assert not by[name].ok and not by[name].fatal


# --------------------------------------------------------------------------- #
# Which version trades
# --------------------------------------------------------------------------- #


def _repos(cfg: BotConfig) -> tuple[Database, Repositories]:
    db = Database(cfg.database_path)
    db.initialize()
    return db, Repositories(db, TradingMode.PAPER)


def test_startup_config_overlays_the_paper_deployment(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    rec = deploy_version(cfg, {"stop_loss.value": 30})
    db, repos = _repos(cfg)
    try:
        eff, versions = resolve_startup_config(cfg, repos, autonomy=A.PAPER_AUTO_IMPROVE)
    finally:
        db.close()
    orb = eff.strategies.strategies[SID]
    assert orb.stop_loss.value == 30 and orb.version == rec.version
    assert versions[SID].source == "DEPLOYED" and versions[SID].version_uid == rec.version_uid
    assert versions["vwap_ema"].source == "YAML"
    assert eff.risk == cfg.risk  # risk limits never change


@pytest.mark.parametrize(("autonomy", "baseline"), [(A.OBSERVE_ONLY, False), (A.PAPER_AUTO_IMPROVE, True)])
def test_observe_only_and_baseline_trade_the_yaml_config(tmp_path: Path, autonomy: AutonomyLevel,
                                                        baseline: bool) -> None:
    cfg = paper_cfg(tmp_path)
    rec = deploy_version(cfg, {"stop_loss.value": 30})
    db, repos = _repos(cfg)
    try:
        eff, versions = resolve_startup_config(cfg, repos, autonomy=autonomy, baseline=baseline)
        assert eff is cfg and versions[SID].source == "YAML" and "not applied" in versions[SID].note
        assert repos.deployments.current(SID).version_uid == rec.version_uid  # type: ignore[union-attr]
    finally:
        db.close()


def test_a_deployment_that_no_longer_fits_the_yaml_falls_back(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    deploy_version(cfg, {"stop_loss.value": 30})
    changed = with_strategy_overrides(cfg, {SID: {"stop_loss": {"mode": "POINTS", "value": 20}}})
    db, repos = _repos(cfg)
    try:
        eff, versions = resolve_startup_config(changed, repos, autonomy=A.PAPER_AUTO_IMPROVE)
    finally:
        db.close()
    assert eff.strategies.strategies[SID] == changed.strategies.strategies[SID]
    assert versions[SID].source == "YAML" and "refused" in versions[SID].note


# --------------------------------------------------------------------------- #
# A session trades the deployed version
# --------------------------------------------------------------------------- #


async def test_a_paper_session_trades_the_deployed_version(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    # YAML stop is 25%; 22% still fits the Rs 2,000 risk budget for one lot at a Rs 100 premium
    rec = deploy_version(cfg, {"stop_loss.value": 22})
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    s = session(cfg, wall, conns)
    bot = await s.start()
    try:
        assert isinstance(bot.broker, PaperBroker) and bot.cfg.mode is TradingMode.PAPER
        assert bot.cfg.strategies.strategies[SID].version == rec.version
        assert conns.feed.started and "NSE_INDEX|Nifty 50" in conns.feed.subscribed
        assert s.repos is not None
        assert s.repos.strategies.get(SID).version == rec.version  # type: ignore[union-attr]
        await drive(s, conns, ORB_DAY)
        (t,) = bot.stack.positions.open_trades(SID)
        assert t.option_type is OptionType.CE
        trade = s.repos.trades.get(t.trade_uid)
        assert trade is not None and trade.strategy_version == rec.version
        assert abs(t.levels.stop / t.levels.entry - 0.78) < 0.01  # the deployed 22% stop, not the YAML 25%
        state = await bot.control.build_state()
        orb = next(x for x in state["strategies"] if x["strategy_id"] == SID)
        assert orb["version"] == rec.version and orb["version_source"] == rec.version_uid
        assert state["connections"]["websocket"] == "CONNECTED"
        assert bot.learning is not None and bot.learning.journal.data_source is EvidenceSource.SYNTHETIC
    finally:
        await s.shutdown()
    assert conns.feed.stopped and conns.closed


async def test_feed_failure_and_upstox_auth_failure_trigger_the_kill_switch(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    s = session(cfg, wall, conns)
    bot = await s.start()
    ks = bot.stack.kill_switch
    try:
        assert conns.hooks is not None
        for fire, reason in ((lambda: conns.feed.emit("RECONNECTING", "FAILED"), KillReason.WEBSOCKET_DISCONNECT),
                             (lambda: conns.hooks.on_circuit_trip("STANDARD"), KillReason.RATE_LIMIT_CIRCUIT),  # type: ignore[union-attr]
                             (lambda: conns.hooks.on_auth_failure("token past daily expiry"),  # type: ignore[union-attr]
                              KillReason.AUTH_FAILURE)):
            fire()
            assert ks.global_event is not None and ks.global_event.reason is reason
            await bot.settle()
            ks.reset(operator="ops.lead", note="test")
    finally:
        await s.shutdown()


async def test_paused_start_and_orderly_shutdown_with_open_trades(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    s = session(cfg, wall, conns, paused=True)
    bot = await s.start()
    assert bot.engine is not None and not bot.engine.accepting_signals
    bot.start()  # the operator starts it (dashboard ENGINE_START)
    await drive(s, conns, ORB_DAY)
    assert bot.stack.positions.open_trade_count(SID) == 1
    s.request_stop("test")
    await s.shutdown()
    await s.shutdown()  # idempotent
    db = Database.open_existing(cfg.database_path)
    try:
        open_rows = Repositories(db, TradingMode.PAPER).trades.list_open()
        assert len(open_rows) == 1 and open_rows[0].status is TradeStatus.OPEN  # restored at the next start
    finally:
        db.close()


async def test_run_returns_after_a_stop_request(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall)
    s = session(cfg, wall, conns)
    asyncio.get_running_loop().call_later(0.2, s.request_stop, "operator")
    result = await asyncio.wait_for(s.run(), timeout=10)
    assert result.reason == "operator" and result.open_trades == 0 and conns.feed.stopped


async def test_stop_at_in_the_past_is_refused(tmp_path: Path) -> None:
    from datetime import time

    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(15, 0))
    s = session(cfg, wall, FakeConnections(wall), stop_at=time(9, 0))
    with pytest.raises(PaperLaunchError, match="already passed"):
        await s.run()


async def test_upstox_connections_are_read_only(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    conn = upstox_connections(cfg, RiskHooks(lambda _: None, lambda _: None))
    try:
        feed = conn.feed
        assert getattr(feed, "portfolio", "missing") is None  # no portfolio stream in PAPER
        assert conn.evidence is EvidenceSource.LIVE_FEED
        assert conn.status()["upstox_connected"] is False  # not connected until started
        from broker.errors import LiveTradingDisabledError

        with pytest.raises(LiveTradingDisabledError):
            await conn.source.place_order()  # type: ignore[attr-defined]
    finally:
        assert conn.aclose is not None
        await conn.aclose()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_check_reports_and_exits_non_zero_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    cfg = paper_cfg(tmp_path)
    no_creds = cfg.model_copy(update={"env": cfg.env.model_copy(update={"upstox_access_token": None})})
    monkeypatch.setattr(cli, "load_config", lambda: no_creds)
    monkeypatch.setattr(cli, "configure_from_config", lambda _cfg: None)
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Upstox credentials" in out and "[PASS] PAPER mode" in out and "YAML" in out
    assert cli.main(["paper"]) == 2  # refuses to start


def test_cli_check_shows_the_deployed_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    cfg = paper_cfg(tmp_path)
    rec = deploy_version(cfg, {"stop_loss.value": 30})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "configure_from_config", lambda _cfg: None)
    cli.main(["check"])
    out = capsys.readouterr().out
    assert f"{SID}: {rec.version} [DEPLOYED]" in out
    cli.main(["check", "--baseline"])
    assert "not applied" in capsys.readouterr().out


def test_no_secret_reaches_the_check_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    cfg = paper_cfg(tmp_path, upstox_access_token=SecretStr("super-secret-token-value"))
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "configure_from_config", lambda _cfg: None)
    cli.main(["check"])
    assert "super-secret-token-value" not in capsys.readouterr().out
