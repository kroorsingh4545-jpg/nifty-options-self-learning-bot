"""VERIFY, PROMOTE / REJECT, PAPER deployment and the loop.

Gate logic runs on FAKE run summaries (hand-made trades, no market data).
Promotion-path tests use FABRICATED validation reports; where a report says
HISTORICAL it is only to exercise the HISTORICAL-only promotion path. No real
or synthetic market data is behind those labels.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any

import pytest

from backtest.backtester import with_ml_enabled
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import BotConfig
from core.autonomy import AutonomyError
from core.clock import IST
from core.enums import (
    AutonomyLevel,
    EvidenceSource,
    LearningStage,
    OptionType,
    OrderSide,
    StrategyVersionStatus,
    TradeStatus,
    TradingMode,
)
from data.storage.records import TradeJournalRecord, TradeRecord
from data.storage.repositories import Repositories
from learning.deployment_controller import DeploymentController, DeploymentError, LiveDeploymentRefused
from learning.hypothesis_generator import HypothesisGenerator
from learning.loop import ReflectiveLoop, render_loop_markdown
from learning.mutation_engine import Candidate, MutationEngine
from learning.promotion_gate import Outcome, PromotionError, PromotionGate, live_confirmation_phrase
from learning.reflection_engine import ReflectionEngine
from learning.validation_engine import (
    GATES,
    GateResult,
    RunSummary,
    ValidationEngine,
    ValidationError,
    ValidationPlan,
    ValidationReport,
    day_types,
)
from tests.ml_support import config
from tests.unit.test_learning_reflect_mutate import row

A = AutonomyLevel
SID = "orb_options"


class Level:
    """A mutable autonomy level (like the dashboard's)."""

    def __init__(self, level: AutonomyLevel = A.PAPER_AUTO_IMPROVE) -> None:
        self.level = level

    def __call__(self) -> AutonomyLevel:
        return self.level


@pytest.fixture(scope="module")
def dataset():  # type: ignore[no-untyped-def]
    # SYNTHETIC: only its calendar is used here (the runs are fakes)
    return generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=8, seed=3))


N = [0]


def trade(d: date, net: float, sid: str = SID) -> TradeRecord:
    N[0] += 1
    t0 = datetime.combine(d, time(10, 0), tzinfo=IST)
    return TradeRecord(trade_uid=f"PAPER-T-{sid}-{N[0]:012d}", mode=TradingMode.PAPER, strategy_id=sid,
                       strategy_version="x", instrument_key="NSE_FO|SYNTH", trading_symbol="SYNTH",
                       option_type=OptionType.CE, strike=25000, expiry=d, side=OrderSide.BUY, quantity=65,
                       entry_price=100, entry_time=t0, exit_price=101, exit_time=t0.replace(hour=11),
                       initial_stop_loss=75, stop_loss=75, net_pnl=net, gross_pnl=net + 40, charges=40,
                       status=TradeStatus.CLOSED, created_at=t0)


def fake_runs(per_day: dict[str, float], *, trades_per_day: int = 3, fail: str | None = None):  # type: ignore[no-untyped-def]
    """who ('production' / 'candidate') -> net per trade on every day."""
    async def run(cfg: BotConfig, days: Sequence[date], label: str) -> RunSummary:
        who, scenario = label.split(":")
        net = per_day[who]
        trades = tuple(trade(d, net) for d in days for _ in range(trades_per_day))
        status = "FAIL" if fail and who == "candidate" and scenario == fail else "PASS"
        return RunSummary(label, tuple(days), trades, status, ("max open trades: 3 > 2",) if status == "FAIL" else ())
    return run


def candidate(cfg: BotConfig | None = None) -> Candidate:
    return MutationEngine(cfg or config()).propose(SID, {"stop_loss.value": 30})


def verify(dataset, run_fn, *, cfg: BotConfig | None = None, cand: Candidate | None = None,  # type: ignore[no-untyped-def]
           plan: ValidationPlan | None = None, **kw: Any) -> ValidationReport:
    eng = ValidationEngine(cfg or config(), dataset, plan=plan or ValidationPlan(min_trades=5), run_fn=run_fn, **kw)
    return asyncio.run(eng.verify(cand or candidate(cfg)))


# --------------------------------------------------------------------------- #
# VERIFY
# --------------------------------------------------------------------------- #


def test_a_clearly_better_candidate_passes_every_gate(dataset) -> None:  # type: ignore[no-untyped-def]
    rep = verify(dataset, fake_runs({"production": 100.0, "candidate": 300.0}))
    assert [g.gate for g in rep.gates] == list(GATES)
    assert rep.passed, rep.failed_gates
    assert rep.evidence_source is EvidenceSource.SYNTHETIC and rep.to_dict()["synthetic"]
    wf = next(g for g in rep.gates if g.gate == "WALK_FORWARD")
    assert len(wf.numbers["folds"]) >= 3  # chronological windows
    oos = next(g for g in rep.gates if g.gate == "OUT_OF_SAMPLE")
    assert len(oos.numbers["days"]) == 2


def test_a_worse_candidate_fails(dataset) -> None:  # type: ignore[no-untyped-def]
    rep = verify(dataset, fake_runs({"production": 200.0, "candidate": -50.0}))
    assert not rep.passed
    assert {"BACKTEST", "WALK_FORWARD", "OUT_OF_SAMPLE", "PRODUCTION_COMPARISON", "NO_TRADE_BASELINE",
            "MULTI_SEED"} <= set(rep.failed_gates)


def test_a_marginal_gain_is_not_enough(dataset) -> None:  # type: ignore[no-untyped-def]
    rep = verify(dataset, fake_runs({"production": 100.0, "candidate": 110.0}))
    assert rep.failed_gates == ["PRODUCTION_COMPARISON"]  # +10 < the 50-rupee minimum gain


def test_any_risk_breach_fails(dataset) -> None:  # type: ignore[no-untyped-def]
    rep = verify(dataset, fake_runs({"production": 100.0, "candidate": 300.0}, fail="stress_thin_quotes"))
    assert rep.failed_gates == ["RISK_COMPLIANCE"]


def test_minimum_sample(dataset) -> None:  # type: ignore[no-untyped-def]
    rep = verify(dataset, fake_runs({"production": 100.0, "candidate": 300.0}, trades_per_day=1),
                 plan=ValidationPlan(min_trades=50))
    assert rep.failed_gates == ["MIN_SAMPLE"]


def test_ml_candidates_need_enough_model_seeds(dataset) -> None:  # type: ignore[no-untyped-def]
    ml_cfg = with_ml_enabled(config(), [SID])
    cand = MutationEngine(ml_cfg).propose(SID, {"ml.confidence_threshold": 0.65})
    runs = fake_runs({"production": 100.0, "candidate": 300.0})
    assert verify(dataset, runs, cfg=ml_cfg, cand=cand).failed_gates == ["MULTI_SEED"]
    assert verify(dataset, runs, cfg=ml_cfg, cand=cand, model_seeds={SID: 5}).passed


def test_candidates_must_be_built_on_production(dataset) -> None:  # type: ignore[no-untyped-def]
    cand = candidate()
    newer = MutationEngine(config()).apply_overlay(SID, cand.config_json)  # production moved on
    with pytest.raises(ValidationError, match="production"):
        verify(dataset, fake_runs({"production": 1.0, "candidate": 2.0}), cfg=newer, cand=cand)


def test_plan_must_fit_the_data(dataset) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        ValidationEngine(config(), dataset, plan=ValidationPlan(holdout_days=8))


def test_day_types(dataset) -> None:  # type: ignore[no-untyped-def]
    types = day_types(dataset, config().settings.instruments.nifty_index_key)
    assert set(types) == {"gap", "high_vol", "expiry"}
    assert types["high_vol"] and all(d.weekday() == 1 for d in types["expiry"])  # SYNTHETIC weeklies: Tuesdays


def test_real_runs_only_enable_the_candidate_strategy(dataset) -> None:  # type: ignore[no-untyped-def]
    """One short real backtest pair on SYNTHETIC data: the candidate and
    production each run alone (strategy isolation)."""
    eng = ValidationEngine(config(), dataset.slice(dataset.days()[:4]),
                           plan=ValidationPlan(train_days=1, test_days=1, holdout_days=1, min_trades=1))
    summary = asyncio.run(eng._run("candidate", candidate().bot_config, "0.1.1", dataset.days()[:2], "main", SID))
    assert {t.strategy_id for t in summary.trades} <= {SID}
    assert summary.compliance_status in ("PASS", "PASS WITH WARNINGS")


# --------------------------------------------------------------------------- #
# PROMOTE / REJECT (fabricated reports)
# --------------------------------------------------------------------------- #


def fabricated(cand: Candidate, *, passed: bool = True,
               evidence: EvidenceSource = EvidenceSource.HISTORICAL) -> ValidationReport:
    gates = [GateResult(g, passed or g != "STRESS", "fabricated for gate logic") for g in GATES]
    return ValidationReport(cand.strategy_id, cand.base_version, cand.version, evidence, gates,
                            {"candidate_oos": {"expectancy": 200.0}})


def setup(paper: Repositories, level: Level) -> tuple[PromotionGate, DeploymentController, Any]:
    cfg = config()
    gate = PromotionGate(paper, cfg, level)
    dep = DeploymentController(paper, cfg, level)
    parent = gate.register_baseline(cfg, SID)
    return gate, dep, parent


def new_candidate(paper: Repositories, gate: PromotionGate, parent: Any, change: dict[str, Any] | None = None
                  ) -> tuple[Candidate, Any]:
    cand = MutationEngine(config()).propose(SID, change or {"stop_loss.value": 30},
                                            taken_versions=paper.strategy_versions.versions(SID))
    h = HypothesisGenerator(config()).generate(ReflectionEngine().reflect_strategy(SID, []))
    return cand, gate.register_candidate(cand, h.hypotheses[0] if h.hypotheses else None, parent)


def test_baseline_is_registered_once(paper: Repositories) -> None:
    gate, _, parent = setup(paper, Level())
    assert parent.status is StrategyVersionStatus.BASELINE and parent.version == "0.1.0"
    assert gate.register_baseline(config(), SID).version_uid == parent.version_uid


def test_failed_candidates_are_rejected_and_archived_with_results(paper: Repositories) -> None:
    gate, _, parent = setup(paper, Level())
    cand, rec = new_candidate(paper, gate, parent)
    d = gate.decide(rec.version_uid, fabricated(cand, passed=False), config())
    assert d.outcome is Outcome.REJECTED and any("STRESS" in r for r in d.reasons)
    got = paper.strategy_versions.get(rec.version_uid)
    assert got is not None and got.status is StrategyVersionStatus.REJECTED
    assert got.evaluation_json["failed_gates"] == ["STRESS"] and len(got.evaluation_json["gates"]) == len(GATES)
    assert any(e.stage is LearningStage.REJECT for e in paper.learning_events.list_for_strategy(SID))
    with pytest.raises(PromotionError):
        gate.decide(rec.version_uid, fabricated(cand), config())  # decided once


def test_synthetic_evidence_never_promotes(paper: Repositories) -> None:
    gate, _, parent = setup(paper, Level())
    cand, rec = new_candidate(paper, gate, parent)
    d = gate.decide(rec.version_uid, fabricated(cand, evidence=EvidenceSource.SYNTHETIC), config())
    assert d.outcome is Outcome.VERIFIED_HELD and "research only" in d.reasons[0]
    assert paper.strategy_versions.get(rec.version_uid).status is StrategyVersionStatus.VERIFIED  # type: ignore[union-attr]


def test_promotion_reads_the_autonomy_level_at_decision_time(paper: Repositories) -> None:
    level = Level(A.OBSERVE_ONLY)
    gate, _, parent = setup(paper, level)
    cand, rec = new_candidate(paper, gate, parent)
    d = gate.decide(rec.version_uid, fabricated(cand), config())
    assert d.outcome is Outcome.VERIFIED_HELD and "OBSERVE_ONLY" in d.reasons[0]
    level.level = A.PAPER_AUTO_IMPROVE
    cand2, rec2 = new_candidate(paper, gate, parent, {"stop_loss.value": 29})
    d2 = gate.decide(rec2.version_uid, fabricated(cand2), config())
    assert d2.outcome is Outcome.PAPER_READY and d2.autonomy is A.PAPER_AUTO_IMPROVE
    got = paper.strategy_versions.get(rec2.version_uid)
    assert got is not None and got.autonomy_level is A.PAPER_AUTO_IMPROVE and got.approved_by is None


def _paper_ready(paper: Repositories, gate: PromotionGate, parent: Any, change: dict[str, Any]) -> Any:
    cand, rec = new_candidate(paper, gate, parent, change)
    assert gate.decide(rec.version_uid, fabricated(cand), config()).outcome is Outcome.PAPER_READY
    return rec


def test_paper_deploy_supersede_rollback_and_retire(paper: Repositories) -> None:
    level = Level()
    gate, dep, parent = setup(paper, level)
    v1 = _paper_ready(paper, gate, parent, {"stop_loss.value": 30})
    dep.deploy_paper(v1.version_uid)
    assert dep.active(SID).version == v1.version  # type: ignore[union-attr]
    eff = dep.effective_config()
    assert eff.strategies.strategies[SID].stop_loss.value == 30 and eff.strategies.strategies[SID].version == v1.version
    assert eff.risk == config().risk  # limits untouched
    v2 = _paper_ready(paper, gate, parent, {"stop_loss.value": 28})
    dep.deploy_paper(v2.version_uid)
    assert paper.strategy_versions.get(v1.version_uid).status is StrategyVersionStatus.SUPERSEDED  # type: ignore[union-attr]
    restored = dep.rollback(SID, actor="a.human", reason="worse in PAPER")
    assert restored is not None and restored.version_uid == v1.version_uid
    assert paper.strategy_versions.get(v2.version_uid).status is StrategyVersionStatus.ROLLED_BACK  # type: ignore[union-attr]
    assert dep.effective_config().strategies.strategies[SID].stop_loss.value == 30
    dep.retire(SID, actor="a.human", reason="back to YAML")
    assert dep.active(SID) is None and dep.effective_config().strategies.strategies[SID].version == "0.1.0"
    with pytest.raises(DeploymentError):
        dep.deploy_paper(v2.version_uid)  # rolled back: never redeployed
    assert [d.action.value for d in paper.deployments.history(SID)] == ["DEPLOY", "DEPLOY", "ROLLBACK", "RETIRE"]


def test_observe_only_blocks_deployment_but_never_rollback(paper: Repositories) -> None:
    level = Level()
    gate, dep, parent = setup(paper, level)
    v1 = _paper_ready(paper, gate, parent, {"stop_loss.value": 30})
    level.level = A.OBSERVE_ONLY
    with pytest.raises(DeploymentError, match="OBSERVE_ONLY"):
        dep.deploy_paper(v1.version_uid)
    level.level = A.PAPER_AUTO_IMPROVE
    dep.deploy_paper(v1.version_uid)
    level.level = A.OBSERVE_ONLY
    assert dep.rollback(SID, actor="a.human", reason="stop learning") is None  # back to the YAML baseline
    assert dep.active(SID) is None


def test_live_deployment_is_impossible(paper: Repositories, live: Repositories) -> None:
    _, dep, _ = setup(paper, Level(A.LIVE_CANDIDATE_APPROVAL))
    with pytest.raises(LiveDeploymentRefused):
        dep.deploy_live("anything")
    with pytest.raises(LiveDeploymentRefused):
        DeploymentController(live, config(), Level())


def _live_feed_rows(version: str, n: int, days: int) -> list[TradeJournalRecord]:
    # FABRICATED journal rows labelled LIVE_FEED, only to exercise the nomination rule
    return [row(300, source=EvidenceSource.LIVE_FEED, day=date(2026, 9, 1 + i % days), minute=i).model_copy(
        update={"strategy_version": version}) for i in range(n)]


def test_live_candidate_needs_the_level_and_a_paper_period(paper: Repositories) -> None:
    level = Level()
    gate, dep, parent = setup(paper, level)
    v1 = _paper_ready(paper, gate, parent, {"stop_loss.value": 30})
    dep.deploy_paper(v1.version_uid)
    rows = _live_feed_rows(v1.version, 25, 12)
    d = gate.nominate_live_candidate(v1.version_uid, rows)
    assert d.outcome is Outcome.NOT_NOMINATED and "does not allow LIVE_CANDIDATE" in " ".join(d.reasons)
    level.level = A.LIVE_CANDIDATE_APPROVAL
    short = gate.nominate_live_candidate(v1.version_uid, _live_feed_rows(v1.version, 25, 3))
    assert short.outcome is Outcome.NOT_NOMINATED and "PAPER days" in " ".join(short.reasons)
    synthetic = [r.model_copy(update={"data_source": EvidenceSource.SYNTHETIC}) for r in rows]
    assert gate.nominate_live_candidate(v1.version_uid, synthetic).outcome is Outcome.NOT_NOMINATED
    assert gate.nominate_live_candidate(v1.version_uid, rows).outcome is Outcome.LIVE_CANDIDATE
    assert paper.strategy_versions.get(v1.version_uid).status is StrategyVersionStatus.LIVE_CANDIDATE  # type: ignore[union-attr]
    assert dep.effective_config().strategies.strategies[SID].version == v1.version  # still PAPER only


def test_live_approval_is_human_only_and_never_deploys(paper: Repositories) -> None:
    level = Level(A.LIVE_CANDIDATE_APPROVAL)
    gate, dep, parent = setup(paper, level)
    v1 = _paper_ready(paper, gate, parent, {"stop_loss.value": 30})
    with pytest.raises(PromotionError, match="LIVE_CANDIDATE"):
        gate.approve_live(v1.version_uid, approved_by="a.human", confirmation="x")
    dep.deploy_paper(v1.version_uid)
    gate.nominate_live_candidate(v1.version_uid, _live_feed_rows(v1.version, 25, 12))
    phrase = live_confirmation_phrase(SID, v1.version)
    for bot in ("learning_loop", "system", ""):
        with pytest.raises(AutonomyError):
            gate.approve_live(v1.version_uid, approved_by=bot, confirmation=phrase)
    with pytest.raises(PromotionError, match="type exactly"):
        gate.approve_live(v1.version_uid, approved_by="a.human", confirmation="yes")
    d = gate.approve_live(v1.version_uid, approved_by="a.human", confirmation=phrase)
    assert d.outcome is Outcome.LIVE_APPROVED
    got = paper.strategy_versions.get(v1.version_uid)
    assert got is not None and got.status is StrategyVersionStatus.LIVE_APPROVED and got.approved_by == "a.human"
    with pytest.raises(LiveDeploymentRefused):
        dep.deploy_live(v1.version_uid)


# --------------------------------------------------------------------------- #
# The loop (fake verification runs)
# --------------------------------------------------------------------------- #


def _journal() -> list[TradeJournalRecord]:
    # SYNTHETIC rows: quick stop-outs -> a STOPS_TOO_TIGHT hypothesis
    return [row(-500, minute=i, hold=5) for i in range(8)] + [row(900, minute=20 + i, exit_reason="TARGET")
                                                              for i in range(4)]


def _loop(paper: Repositories, dataset, level: Level, *, historical: bool) -> ReflectiveLoop:  # type: ignore[no-untyped-def]
    def build(prod: BotConfig) -> ValidationEngine:
        eng = ValidationEngine(prod, dataset, plan=ValidationPlan(min_trades=5),
                               run_fn=fake_runs({"production": 100.0, "candidate": 300.0}))
        if historical:
            eng.evidence = EvidenceSource.HISTORICAL  # FABRICATED label: fake runs, exercises the promotion path
        return eng
    return ReflectiveLoop(config(), paper, validation=build, autonomy_fn=level)


def test_loop_on_synthetic_evidence_promotes_nothing(paper: Repositories, dataset) -> None:  # type: ignore[no-untyped-def]
    res = asyncio.run(_loop(paper, dataset, Level(), historical=False).run(_journal(), date(2026, 11, 2),
                                                                             date(2026, 11, 2), strategy_ids=[SID]))
    run = res.strategies[SID]
    assert run.candidates and all(c.outcome in ("VERIFIED_HELD", "REJECTED", "REFUSED") for c in run.candidates)
    assert run.deployed is None and paper.deployments.current(SID) is None
    stages = {e.stage for e in paper.learning_events.list_for_strategy(SID)}
    assert {LearningStage.LOG, LearningStage.REFLECT, LearningStage.HYPOTHESIZE, LearningStage.MUTATE,
            LearningStage.VERIFY} <= stages
    md = render_loop_markdown(res)
    assert md.startswith("> **SYNTHETIC") and "LIVE always needs explicit human approval" in md


def test_loop_auto_deploys_to_paper_only_when_allowed(paper: Repositories, dataset) -> None:  # type: ignore[no-untyped-def]
    level = Level(A.OBSERVE_ONLY)
    res = asyncio.run(_loop(paper, dataset, level, historical=True).run(_journal(), date(2026, 11, 2),
                                                                          date(2026, 11, 2), strategy_ids=[SID]))
    assert res.strategies[SID].deployed is None
    level.level = A.PAPER_AUTO_IMPROVE
    res = asyncio.run(_loop(paper, dataset, level, historical=True).run(_journal(), date(2026, 11, 2),
                                                                          date(2026, 11, 2), strategy_ids=[SID]))
    run = res.strategies[SID]
    assert run.deployed is not None and any(c.outcome == "PAPER_READY" for c in run.candidates)
    active = DeploymentController(paper, config(), level).active(SID)
    assert active is not None and active.status is StrategyVersionStatus.PAPER_ACTIVE
    assert run.nominated == []  # LIVE nominations need LIVE_CANDIDATE_APPROVAL and a paper period


# --------------------------------------------------------------------------- #
# CLI (SYNTHETIC research run)
# --------------------------------------------------------------------------- #


def test_cli_synthetic_run_never_touches_the_trading_database(config_dir, edit_yaml, tmp_path,  # type: ignore[no-untyped-def]
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from learning.__main__ import main

    def point(d: dict) -> None:  # type: ignore[type-arg]
        d["learning"]["research_dir"] = str(tmp_path / "research")
        d["database"]["path"] = str(tmp_path / "trading.db")

    edit_yaml("settings.yaml", point)
    out = tmp_path / "report"
    rc = main(["synthetic", "--config-dir", str(config_dir), "--lot-units", "65", "--paper-days", "3",
               "--verify-days", "5", "--holdout-days", "1", "--min-trades", "1", "--out", str(out)])
    assert rc == 0 and "SYNTHETIC" in capsys.readouterr().out
    assert not (tmp_path / "trading.db").exists()
    dbs = list((tmp_path / "research").rglob("learning.db"))
    assert len(dbs) == 1
    md = (out / "learning_report.md").read_text(encoding="utf-8")
    assert md.startswith("> **SYNTHETIC")
    data = json.loads((out / "learning_report.json").read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    outcomes = {c["outcome"] for s in data["strategies"].values() for c in s["candidates"]}
    assert "PAPER_READY" not in outcomes and all(s["deployed"] is None for s in data["strategies"].values())
