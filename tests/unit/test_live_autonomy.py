"""The learning loop reads the running bot's autonomy level (Phase 9 warning 2).

* In-process: ``LearningLoopRunner`` reads ``bot.mode.autonomy`` at every
  promotion step, so a dashboard switch mid-run takes effect at once.
* Another process: ``PublishedAutonomy`` reads the bot's heartbeat and falls
  back to the persisted level (fail-safe) when the bot is not running.
* Restarts: the bot starts with the most restrictive of YAML and the last
  dashboard change.

Verification runs are FAKES (hand-made trades); the HISTORICAL evidence label
on the validation engine is FABRICATED to exercise the promotion path.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backtest.data import save_dataset
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import BotConfig
from core.autonomy import most_restrictive
from core.enums import AutonomyLevel, ControlCommandStatus, ControlCommandType, EvidenceSource, TradingMode
from data.storage.database import Database
from data.storage.records import ControlCommandRecord
from data.storage.repositories import Repositories
from learning.autonomy_source import FAIL_SAFE, PublishedAutonomy, persisted_autonomy
from learning.loop import LoopRefused, load_verification_data
from learning.validation_engine import RunSummary, ValidationEngine, ValidationPlan
from runtime.learning_loop import LearningLoopRunner
from tests.fakes import FakeWallClock
from tests.paper_support import SID, FakeConnections, paper_cfg, session
from tests.synthetic import at
from tests.unit.test_learning_verify_promote import _journal as journal_rows
from tests.unit.test_learning_verify_promote import fake_runs

A = AutonomyLevel


def test_most_restrictive_orders_the_levels() -> None:
    assert most_restrictive(A.LIVE_CANDIDATE_APPROVAL, A.PAPER_AUTO_IMPROVE) is A.PAPER_AUTO_IMPROVE
    assert most_restrictive(A.PAPER_AUTO_IMPROVE, A.OBSERVE_ONLY) is A.OBSERVE_ONLY
    assert most_restrictive("LIVE_CANDIDATE_APPROVAL") is A.LIVE_CANDIDATE_APPROVAL
    with pytest.raises(ValueError):
        most_restrictive()


# --------------------------------------------------------------------------- #
# Persisted level (restarts) and the heartbeat reader (another process)
# --------------------------------------------------------------------------- #


def _repos(cfg: BotConfig) -> tuple[Database, Repositories]:
    db = Database(cfg.database_path)
    db.initialize()
    return db, Repositories(db, TradingMode.PAPER)


def _command(repos: Repositories, level: str, status: ControlCommandStatus) -> None:
    cid = repos.commands.enqueue(ControlCommandRecord(mode=TradingMode.PAPER, command=ControlCommandType.SET_AUTONOMY,
                                                      operator="ops.lead", payload_json={"level": level}))
    repos.commands.complete(cid, status, "test", at=at(10, 0))


def test_persisted_level_is_yaml_capped_by_the_last_dashboard_change(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)  # YAML: PAPER_AUTO_IMPROVE
    db, repos = _repos(cfg)
    try:
        assert persisted_autonomy(repos, cfg) is A.PAPER_AUTO_IMPROVE
        _command(repos, "OBSERVE_ONLY", ControlCommandStatus.DONE)
        assert persisted_autonomy(repos, cfg) is A.OBSERVE_ONLY  # survives a restart
        _command(repos, "PAPER_AUTO_IMPROVE", ControlCommandStatus.REJECTED)
        assert persisted_autonomy(repos, cfg) is A.OBSERVE_ONLY  # only applied changes count
        _command(repos, "LIVE_CANDIDATE_APPROVAL", ControlCommandStatus.DONE)
        assert persisted_autonomy(repos, cfg) is A.PAPER_AUTO_IMPROVE  # YAML is the ceiling
    finally:
        db.close()


def test_published_level_follows_the_heartbeat_and_fails_safe(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    db, repos = _repos(cfg)
    wall = FakeWallClock(at(16, 0))
    try:
        src = PublishedAutonomy(repos, cfg, max_age_seconds=10, now_fn=wall)
        assert src() is A.PAPER_AUTO_IMPROVE and "not running" in src.last_source  # no heartbeat yet
        repos.runtime_state.publish({"mode": "PAPER", "autonomy": "OBSERVE_ONLY"}, at=wall())
        assert src() is A.OBSERVE_ONLY and "running bot" in src.last_source
        repos.runtime_state.publish({"mode": "PAPER", "autonomy": "LIVE_CANDIDATE_APPROVAL"}, at=wall())
        assert src() is A.LIVE_CANDIDATE_APPROVAL  # the running session's level, read on every call
        wall.advance(seconds=30)
        assert src() is A.PAPER_AUTO_IMPROVE  # stale heartbeat: the persisted level
        repos.runtime_state.publish({"mode": "PAPER", "autonomy": "TURBO"}, at=wall())
        assert src() is FAIL_SAFE
    finally:
        db.close()


def test_published_level_is_paper_only(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    db = Database(cfg.database_path)
    db.initialize()
    try:
        with pytest.raises(ValueError, match="PAPER"):
            PublishedAutonomy(Repositories(db, TradingMode.LIVE), cfg)
    finally:
        db.close()


async def test_a_paper_session_starts_with_the_persisted_level(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    db, repos = _repos(cfg)
    _command(repos, "OBSERVE_ONLY", ControlCommandStatus.DONE)
    db.close()
    wall = FakeWallClock(at(9, 14))
    s = session(cfg, wall, FakeConnections(wall))
    bot = await s.start()
    try:
        assert bot.mode.autonomy is A.OBSERVE_ONLY and s.autonomy is A.OBSERVE_ONLY
        assert all(v.source == "YAML" for v in s.versions.values())  # OBSERVE_ONLY runs the YAML baseline
    finally:
        await s.shutdown()


# --------------------------------------------------------------------------- #
# In-process loop next to the running bot
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def dataset():  # type: ignore[no-untyped-def]
    # SYNTHETIC: only its calendar is used (the verification runs are fakes)
    return generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=8, seed=3))


def _enqueue(repos: Repositories, level: str) -> int:
    # the control plane runs on the real wall clock; no expiry keeps the test clock-independent
    return repos.commands.enqueue(ControlCommandRecord(
        mode=TradingMode.PAPER, command=ControlCommandType.SET_AUTONOMY, operator="ops.lead",
        payload_json={"level": level}, expires_at=None))


async def _run_loop(tmp_path: Path, dataset: Any, *, switch_mid_run: bool) -> tuple[Any, Any, Repositories]:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(16, 0))  # after the close
    s = session(cfg, wall, FakeConnections(wall))
    bot = await s.start()
    assert s.repos is not None
    repos = s.repos
    runner = LearningLoopRunner(bot, cfg)
    assert runner.refusal() is None and runner.autonomy() is A.PAPER_AUTO_IMPROVE
    inner = fake_runs({"production": 100.0, "candidate": 300.0})
    switched = False

    async def run_fn(c: BotConfig, days: Any, label: str) -> RunSummary:
        nonlocal switched
        if switch_mid_run and not switched:  # the operator switches the dashboard while VERIFY runs
            switched = True
            _enqueue(repos, "OBSERVE_ONLY")
            await bot.control.process_pending()
        return await inner(c, days, label)

    def build(prod: BotConfig) -> ValidationEngine:
        eng = ValidationEngine(prod, dataset, plan=ValidationPlan(min_trades=5), run_fn=run_fn)
        eng.evidence = EvidenceSource.HISTORICAL  # FABRICATED label (fake runs): exercises promotion
        return eng

    try:
        res = await runner.build_loop(repos, build).run(journal_rows(), date(2026, 11, 2), date(2026, 11, 2),
                                                        strategy_ids=[SID])
        return res, bot.mode.autonomy, repos
    finally:
        await s.shutdown()


async def test_the_loop_promotes_and_deploys_at_the_bots_live_level(tmp_path: Path, dataset: Any) -> None:
    res, level, _ = await _run_loop(tmp_path, dataset, switch_mid_run=False)
    run = res.strategies[SID]
    assert level is A.PAPER_AUTO_IMPROVE
    assert run.deployed is not None and any(c.outcome == "PAPER_READY" for c in run.candidates)


async def test_a_dashboard_switch_mid_run_stops_promotion_at_once(tmp_path: Path, dataset: Any) -> None:
    res, level, repos = await _run_loop(tmp_path, dataset, switch_mid_run=True)
    run = res.strategies[SID]
    assert level is A.OBSERVE_ONLY and res.autonomy is A.OBSERVE_ONLY
    assert run.candidates and not any(c.outcome == "PAPER_READY" for c in run.candidates)
    assert any("OBSERVE_ONLY" in " ".join(c.reasons) for c in run.candidates)
    assert run.deployed is None and repos.deployments.current(SID) is None


async def test_the_runner_refuses_during_market_hours_or_with_open_trades(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    wall = FakeWallClock(at(10, 0))
    s = session(cfg, wall, FakeConnections(wall))
    bot = await s.start()
    try:
        runner = LearningLoopRunner(bot, cfg)
        assert runner.refusal() == "the market is open"
        assert await runner.run_once() is None
    finally:
        await s.shutdown()


def test_the_runner_refuses_synthetic_verification_data(tmp_path: Path, dataset: Any) -> None:
    out = save_dataset(dataset, tmp_path / "synthetic-ds")
    with pytest.raises(LoopRefused, match="HISTORICAL"):
        load_verification_data(out)
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "SYNTHETIC"


def test_cli_run_refuses_synthetic_data_and_market_hours(tmp_path: Path, dataset: Any,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import learning.__main__ as lm

    cfg = paper_cfg(tmp_path)
    out = save_dataset(dataset, tmp_path / "synthetic-ds")
    monkeypatch.setattr(lm, "load_config", lambda _d=None: cfg)
    monkeypatch.setattr(lm, "now_ist", lambda: at(16, 0))
    with pytest.raises(SystemExit, match="HISTORICAL"):
        lm.main(["run", "--data", str(out)])
    monkeypatch.setattr(lm, "now_ist", lambda: at(11, 0))
    with pytest.raises(SystemExit, match="market is open"):
        lm.main(["run", "--data", str(out)])
    assert not cfg.database_path.exists()  # nothing was created
