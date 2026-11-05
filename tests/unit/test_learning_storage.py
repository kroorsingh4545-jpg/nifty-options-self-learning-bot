"""Phase 9 storage and policy: autonomy table, schema v5 rules (RL decisions,
trade journal, strategy versions, PAPER-only deployments), and persistence
of every RL decision (Phase 8 audit warning 2). No market data is used
except the SYNTHETIC backtest dataset at the end."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from backtest.backtester import Backtester, BacktestOptions
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from core.autonomy import (
    AutonomyError,
    is_human_approver,
    may_auto_deploy_paper,
    may_nominate_live_candidate,
    may_promote_to_paper,
    require_live_nomination,
    require_paper_promotion,
)
from core.clock import IST
from core.enums import (
    AutonomyLevel,
    DeploymentAction,
    EvidenceSource,
    OptionType,
    OrderSide,
    RLDecisionKind,
    StrategyVersionStatus,
    TradeStatus,
    TradingMode,
)
from core.ids import new_trade_uid, new_uid
from data.storage.records import (
    RLDecisionRecord,
    StrategyDeploymentRecord,
    StrategyVersionRecord,
    TradeJournalRecord,
    TradeRecord,
)
from data.storage.repositories import Repositories
from ml.rl_environment import Action
from tests.ml_support import config
from tests.unit.test_rl_inference import StubRL, _sig, advisor, ctx, rl_stub

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=IST)
T1 = datetime(2026, 11, 4, 10, 30, tzinfo=IST)
A = AutonomyLevel

# --------------------------------------------------------------------------- #
# Autonomy policy
# --------------------------------------------------------------------------- #


def test_autonomy_policy_table() -> None:
    assert [may_promote_to_paper(x) for x in A] == [False, True, True]
    assert [may_auto_deploy_paper(x) for x in A] == [False, True, True]
    assert [may_nominate_live_candidate(x) for x in A] == [False, False, True]
    with pytest.raises(AutonomyError, match="OBSERVE_ONLY"):
        require_paper_promotion(A.OBSERVE_ONLY)
    with pytest.raises(AutonomyError, match="PAPER_AUTO_IMPROVE"):
        require_live_nomination(A.PAPER_AUTO_IMPROVE)
    assert require_paper_promotion("PAPER_AUTO_IMPROVE") is A.PAPER_AUTO_IMPROVE
    with pytest.raises(ValueError):
        require_paper_promotion("LIVE")


def test_default_autonomy_is_paper_auto_improve() -> None:
    assert config().settings.app.autonomy_level is A.PAPER_AUTO_IMPROVE


@pytest.mark.parametrize("name", ["", "  ", "learning_loop", "Bot", "SYSTEM", "auto", None])
def test_non_human_approvers(name: str | None) -> None:
    assert not is_human_approver(name)


def test_human_approver() -> None:
    assert is_human_approver("a.human")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _closed_trade(repos: Repositories, sid: str = "orb_options") -> TradeRecord:
    uid = new_trade_uid(TradingMode.PAPER, sid)
    t = TradeRecord(trade_uid=uid, mode=TradingMode.PAPER, strategy_id=sid, strategy_version="0.1.0",
                    instrument_key="NSE_FO|1", trading_symbol="NIFTY SYNTH CE", option_type=OptionType.CE,
                    strike=25000, expiry=date(2026, 11, 10), side=OrderSide.BUY, quantity=65, entry_price=100,
                    entry_time=T0, initial_stop_loss=75, stop_loss=75, target=150)
    repos.trades.insert(t)
    repos.trades.close(uid, exit_price=120, exit_reason="TARGET", gross_pnl=1300, charges=60, r_multiple=0.8)
    got = repos.trades.get(uid)
    assert got is not None and got.status is TradeStatus.CLOSED
    return got


def _journal(t: TradeRecord, **over: Any) -> TradeJournalRecord:
    base: dict[str, Any] = dict(
        trade_uid=t.trade_uid, mode=t.mode, strategy_id=t.strategy_id, strategy_version=t.strategy_version,
        data_source=EvidenceSource.SYNTHETIC, instrument_key=t.instrument_key, trading_symbol=t.trading_symbol,
        option_type=t.option_type, strike=t.strike, expiry=t.expiry, quantity=t.quantity, entry_time=T0,
        exit_time=T1, entry_price=100, exit_price=120, initial_stop_loss=75, final_stop_loss=75, gross_pnl=1300,
        costs=60, net_pnl=1240)
    base.update(over)
    return TradeJournalRecord(**base)


def _version(sid: str = "orb_options", version: str = "0.1.1",
             status: StrategyVersionStatus = StrategyVersionStatus.CANDIDATE) -> StrategyVersionRecord:
    return StrategyVersionRecord(version_uid=new_uid("SV"), strategy_id=sid, version=version, base_version="0.1.0",
                                 status=status, config_json={"version": version})


def _rl(**over: Any) -> RLDecisionRecord:
    base: dict[str, Any] = dict(decision_uid=new_uid("RLD"), mode=TradingMode.PAPER, strategy_id="orb_options",
                                kind=RLDecisionKind.ENTRY, signal_id="SIG-1", action="ENTER", allowed=True,
                                decided_at=T0)
    base.update(over)
    return RLDecisionRecord(**base)


# --------------------------------------------------------------------------- #
# rl_decisions
# --------------------------------------------------------------------------- #


def test_rl_decisions_round_trip_and_are_append_only(paper: Repositories) -> None:
    t = _closed_trade(paper)
    paper.rl_decisions.insert(_rl())
    paper.rl_decisions.insert(_rl(kind=RLDecisionKind.MANAGE, signal_id=None, trade_uid=t.trade_uid,
                                  action="TIGHTEN_STOP", allowed=None, applied=True, result="ok", new_stop=90.0,
                                  confidence=0.7, model_version="m1"))
    rows = paper.rl_decisions.list_for_strategy("orb_options")
    assert [r.kind for r in rows] == [RLDecisionKind.ENTRY, RLDecisionKind.MANAGE]
    assert paper.rl_decisions.list_for_trade(t.trade_uid)[0].applied is True
    assert paper.rl_decisions.count() == 2
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        paper.db.execute("UPDATE rl_decisions SET action = 'X'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        paper.db.execute("DELETE FROM rl_decisions")


def test_rl_decision_rules(paper: Repositories, live: Repositories) -> None:
    with pytest.raises(ValidationError, match="trade"):
        _rl(kind=RLDecisionKind.MANAGE, signal_id=None)
    with pytest.raises(ValidationError, match="signal"):
        _rl(signal_id=None)
    with pytest.raises(ValidationError):
        _rl(confidence=1.5)
    t = _closed_trade(paper)
    with pytest.raises(sqlite3.IntegrityError, match="isolation"):  # a decision cannot attach to another strategy
        paper.rl_decisions.insert(_rl(strategy_id="vwap_ema", kind=RLDecisionKind.MANAGE, trade_uid=t.trade_uid,
                                      signal_id=None, action="HOLD"))
    with pytest.raises(Exception, match="bound to LIVE"):
        live.rl_decisions.insert(_rl())  # PAPER record into a LIVE-bound repository


# --------------------------------------------------------------------------- #
# trade_journal
# --------------------------------------------------------------------------- #


def test_journal_needs_a_closed_trade_and_is_append_only(paper: Repositories) -> None:
    t = _closed_trade(paper)
    assert paper.journal.insert(_journal(t))
    assert not paper.journal.insert(_journal(t))  # idempotent
    got = paper.journal.get(t.trade_uid)
    assert got is not None and got.data_source is EvidenceSource.SYNTHETIC and not got.is_real_evidence
    assert [e.trade_uid for e in paper.journal.list("orb_options", since=T0)] == [t.trade_uid]
    assert paper.journal.list("orb_options", until=T0) == []
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        paper.db.execute("UPDATE trade_journal SET net_pnl = 1e9")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        paper.db.execute("DELETE FROM trade_journal")
    open_uid = new_trade_uid(TradingMode.PAPER, "orb_options")
    paper.trades.insert(t.model_copy(update={"trade_uid": open_uid, "status": TradeStatus.OPEN, "exit_price": None,
                                             "exit_time": None, "net_pnl": None, "id": None}))
    with pytest.raises(sqlite3.IntegrityError, match="CLOSED"):
        paper.journal.insert(_journal(t, trade_uid=open_uid))
    with pytest.raises(ValidationError, match="entry_time"):
        _journal(t, entry_time=T1, exit_time=T0)


# --------------------------------------------------------------------------- #
# strategy_versions
# --------------------------------------------------------------------------- #


def test_versions_start_as_baseline_or_candidate(paper: Repositories) -> None:
    with pytest.raises(ValueError, match="BASELINE or CANDIDATE"):
        paper.strategy_versions.insert(_version(status=StrategyVersionStatus.VERIFIED))
    with pytest.raises(sqlite3.IntegrityError, match="BASELINE or CANDIDATE"):
        paper.db.execute("INSERT INTO strategy_versions (version_uid, strategy_id, version, status, config_json, "
                         "created_at, updated_at) VALUES ('X', 'orb_options', '9.9.9', 'PAPER_READY', '{}', 'a', 'a')")


def _promote_historical(paper: Repositories, v: StrategyVersionRecord, level: AutonomyLevel = A.PAPER_AUTO_IMPROVE
                        ) -> None:
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.VERIFIED, reason="gates passed",
                                       evidence_source=EvidenceSource.HISTORICAL, evaluation={"gates_passed": True})
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.PAPER_READY, reason="gate",
                                       autonomy=level)


def test_synthetic_verified_versions_can_never_be_promoted(paper: Repositories) -> None:
    v = _version()
    paper.strategy_versions.insert(v)
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.VERIFIED, reason="gates passed",
                                       evidence_source=EvidenceSource.SYNTHETIC)
    with pytest.raises(ValueError, match="HISTORICAL"):
        paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.PAPER_READY, reason="x",
                                           autonomy=A.PAPER_AUTO_IMPROVE)
    with pytest.raises(sqlite3.IntegrityError, match="HISTORICAL"):
        paper.db.execute("UPDATE strategy_versions SET status = 'PAPER_READY', autonomy_level = 'PAPER_AUTO_IMPROVE'"
                         " WHERE version_uid = ?", (v.version_uid,))
    with pytest.raises(sqlite3.IntegrityError, match="set once"):  # evidence cannot be relabelled
        paper.db.execute("UPDATE strategy_versions SET evidence_source = 'HISTORICAL' WHERE version_uid = ?",
                         (v.version_uid,))


def test_paper_ready_needs_an_autonomy_level_that_allows_it(paper: Repositories) -> None:
    v = _version()
    paper.strategy_versions.insert(v)
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.VERIFIED, reason="ok",
                                       evidence_source=EvidenceSource.HISTORICAL)
    for level in (None, "OBSERVE_ONLY"):
        with pytest.raises(sqlite3.IntegrityError, match="autonomy"):
            paper.db.execute("UPDATE strategy_versions SET status = 'PAPER_READY', autonomy_level = ? "
                             "WHERE version_uid = ?", (level, v.version_uid))
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.PAPER_READY, reason="ok",
                                       autonomy=A.PAPER_AUTO_IMPROVE)
    got = paper.strategy_versions.get(v.version_uid)
    assert got is not None and got.status is StrategyVersionStatus.PAPER_READY
    assert got.autonomy_level is A.PAPER_AUTO_IMPROVE


def test_live_candidate_needs_live_candidate_approval_and_live_needs_a_human(paper: Repositories) -> None:
    v = _version()
    paper.strategy_versions.insert(v)
    _promote_historical(paper, v)
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.PAPER_ACTIVE, reason="deployed",
                                       autonomy=A.PAPER_AUTO_IMPROVE)
    with pytest.raises(sqlite3.IntegrityError, match="autonomy"):
        paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.LIVE_CANDIDATE, reason="x",
                                           autonomy=A.PAPER_AUTO_IMPROVE)
    paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.LIVE_CANDIDATE, reason="paper ok",
                                       autonomy=A.LIVE_CANDIDATE_APPROVAL)
    with pytest.raises(ValueError, match="human"):
        paper.strategy_versions.set_status(v.version_uid, StrategyVersionStatus.LIVE_APPROVED, reason="x")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        paper.db.execute("UPDATE strategy_versions SET status = 'LIVE_APPROVED' WHERE version_uid = ?",
                         (v.version_uid,))


@pytest.mark.parametrize(("start", "target"), [
    (StrategyVersionStatus.CANDIDATE, StrategyVersionStatus.PAPER_READY),
    (StrategyVersionStatus.CANDIDATE, StrategyVersionStatus.PAPER_ACTIVE),
    (StrategyVersionStatus.CANDIDATE, StrategyVersionStatus.LIVE_APPROVED),
    (StrategyVersionStatus.BASELINE, StrategyVersionStatus.PAPER_ACTIVE),
])
def test_status_transitions_cannot_skip_stages(paper: Repositories, start: StrategyVersionStatus,
                                               target: StrategyVersionStatus) -> None:
    v = _version(status=start)
    paper.strategy_versions.insert(v)
    paper.db.execute("UPDATE strategy_versions SET evidence_source = 'HISTORICAL', autonomy_level = "
                     "'LIVE_CANDIDATE_APPROVAL', approved_by = 'a.human' WHERE version_uid = ?", (v.version_uid,))
    with pytest.raises(sqlite3.IntegrityError, match="transition"):
        paper.db.execute("UPDATE strategy_versions SET status = ? WHERE version_uid = ?", (target.value,
                                                                                          v.version_uid))


def test_a_version_definition_is_immutable(paper: Repositories) -> None:
    v = _version()
    paper.strategy_versions.insert(v)
    for col, val in (("config_json", '{"stop": 1}'), ("mutation_json", "[]1"), ("version", "0.2.0"),
                     ("strategy_id", "vwap_ema")):
        with pytest.raises(sqlite3.IntegrityError):
            paper.db.execute(f"UPDATE strategy_versions SET {col} = ? WHERE version_uid = ?",  # noqa: S608
                             (val, v.version_uid))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        paper.db.execute("DELETE FROM strategy_versions")


# --------------------------------------------------------------------------- #
# strategy_deployments
# --------------------------------------------------------------------------- #


def _dep(v: StrategyVersionRecord | None, **over: Any) -> StrategyDeploymentRecord:
    base: dict[str, Any] = dict(deployment_uid=new_uid("DEP"), strategy_id="orb_options",
                                action=DeploymentAction.DEPLOY, version_uid=v.version_uid if v else None,
                                actor="learning_loop", autonomy_level=A.PAPER_AUTO_IMPROVE)
    base.update(over)
    return StrategyDeploymentRecord(**base)


def test_live_deployments_cannot_exist(paper: Repositories) -> None:
    with pytest.raises(ValidationError, match="PAPER"):
        _dep(None, mode=TradingMode.LIVE, action=DeploymentAction.RETIRE)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        paper.db.execute("INSERT INTO strategy_deployments (deployment_uid, mode, strategy_id, action, actor, "
                         "autonomy_level, created_at) VALUES ('D', 'LIVE', 'orb_options', 'RETIRE', 'x', "
                         "'LIVE_CANDIDATE_APPROVAL', 'a')")


def test_only_historical_paper_ready_versions_deploy(paper: Repositories) -> None:
    cand = _version()
    paper.strategy_versions.insert(cand)
    with pytest.raises(sqlite3.IntegrityError, match="deployment"):
        paper.deployments.insert(_dep(cand))  # still a CANDIDATE
    _promote_historical(paper, cand)
    with pytest.raises(sqlite3.IntegrityError, match="autonomy"):
        paper.deployments.insert(_dep(cand, autonomy_level=A.OBSERVE_ONLY))
    with pytest.raises(sqlite3.IntegrityError, match="deployment"):  # another strategy's version
        paper.deployments.insert(_dep(cand, strategy_id="vwap_ema"))
    paper.deployments.insert(_dep(cand))
    cur = paper.deployments.current("orb_options")
    assert cur is not None and cur.version_uid == cand.version_uid
    paper.deployments.insert(_dep(None, action=DeploymentAction.RETIRE, previous_version_uid=cand.version_uid,
                                  autonomy_level=A.OBSERVE_ONLY))  # going back to the baseline is always allowed
    assert paper.deployments.current("orb_options").version_uid is None  # type: ignore[union-attr]
    assert len(paper.deployments.history("orb_options")) == 2
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        paper.db.execute("UPDATE strategy_deployments SET actor = 'x'")


# --------------------------------------------------------------------------- #
# Every RL decision is persisted (Phase 8 audit warning 2)
# --------------------------------------------------------------------------- #


def test_entry_decisions_are_persisted(paper: Repositories) -> None:
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.ENTER))})
    a.decisions = paper.rl_decisions
    assert a.evaluate(_sig()).allow
    a2, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.SKIP_SETUP))})
    a2.decisions = paper.rl_decisions
    assert not a2.evaluate(_sig()).allow
    a3, _ = advisor({})
    a3.decisions = paper.rl_decisions
    assert not a3.evaluate(_sig()).allow  # no model: fail-closed veto, also recorded
    assert a3.evaluate(_sig("vwap_ema")).allow  # RL off for vwap_ema: not an RL decision, not recorded
    rows = paper.rl_decisions.list_for_strategy("orb_options")
    assert [(r.action, r.allowed) for r in rows] == [("ENTER", True), ("SKIP_SETUP", False), ("VETO", False)]
    assert rows[0].confidence == 0.9 and rows[0].model_version == "stub-rl-orb_options-synthetic"
    assert all(r.kind is RLDecisionKind.ENTRY and r.research for r in rows)
    assert paper.rl_decisions.list_for_strategy("vwap_ema") == []


@dataclass
class BrokenLog:
    mode: TradingMode = TradingMode.PAPER
    calls: int = 0

    def insert(self, rec: Any) -> int:
        self.calls += 1
        raise sqlite3.OperationalError("disk I/O error")


def test_an_unrecorded_entry_approval_is_vetoed_and_rl_suspended() -> None:
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.ENTER))})
    log = BrokenLog()
    a.decisions = log  # type: ignore[assignment]
    d = a.evaluate(_sig())
    assert not d.allow and "could not be recorded" in d.reason
    assert "orb_options" in a.suspended and a.record_failures == 1
    assert not a.evaluate(_sig()).allow  # stays refused for the session
    assert a.advise(ctx()).action.value == "HOLD"  # trades keep their deterministic stops


def test_advisor_refuses_a_decision_log_of_another_mode(live: Repositories) -> None:
    from ml.inference_service import InferenceConfigError, RLTradeAdvisor

    a, _ = advisor({})
    with pytest.raises(InferenceConfigError, match="bound to LIVE"):
        RLTradeAdvisor(a.cfg, features=a.features, models={}, option_context=lambda s, t: None,
                       account=lambda s: None,  # type: ignore[arg-type,return-value]
                       outcomes=lambda s: (), decisions=live.rl_decisions)


def test_every_rl_decision_in_a_backtest_is_persisted() -> None:
    """SYNTHETIC data, stub policy: ENTER every setup, then TRAIL_STOP. Every
    entry approval and every per-minute management decision (HOLD included)
    lands in rl_decisions with execution's result."""
    ds = generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=2, seed=14,
                                                  strike_margin_points=200))

    @dataclass
    class EnterThenTrail(StubRL):
        def act(self, raw: np.ndarray, mask: np.ndarray) -> tuple[Action, dict[str, Any]]:
            if mask[Action.ENTER.value]:
                return Action.ENTER, {"confidence": 1.0}
            return (Action.TRAIL_STOP if mask[Action.TRAIL_STOP.value] else Action.HOLD), {"confidence": 0.8}

    sids = tuple(s for s, c in config().strategies.strategies.items() if c.enabled)
    models = {s: rl_stub(s, EnterThenTrail(Action.TRAIL_STOP)) for s in sids}
    res = asyncio.run(Backtester(config(), ds, BacktestOptions(rl_strategies=sids, rl_models=models,
                                                               rl_research=True)).run())
    assert res.trades
    entries = [d for d in res.rl_decisions if d.kind is RLDecisionKind.ENTRY]
    manage = [d for d in res.rl_decisions if d.kind is RLDecisionKind.MANAGE]
    # one ENTRY decision per signal that reached the RL filter, each approved
    assert entries and all(d.allowed for d in entries)
    assert len(entries) >= len(res.trades)
    traded = {t.trade_uid for t in res.trades}
    assert manage and {d.trade_uid for d in manage} <= traded
    assert all(d.applied is not None or d.action == "HOLD" for d in manage)
    assert any(d.action == "TRAIL_STOP" and d.applied for d in manage)
    assert all(d.decided_at.date() in res.days for d in res.rl_decisions)  # simulated time, not wall clock



# --------------------------------------------------------------------------- #
# Audit warning 1: an invalid RL record never escapes; it suspends RL
# --------------------------------------------------------------------------- #


def test_an_invalid_entry_record_vetoes_without_raising(monkeypatch: pytest.MonkeyPatch,
                                                        paper: Repositories) -> None:
    from ml.inference_service import RLTradeAdvisor

    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.ENTER))})
    a.decisions = paper.rl_decisions

    def broken(self: RLTradeAdvisor, **kw: Any) -> RLDecisionRecord:
        raise ValueError("injected: invalid record")

    monkeypatch.setattr(RLTradeAdvisor, "decision_record", broken)
    d = a.evaluate(_sig())  # must not raise
    assert not d.allow and "could not be recorded" in d.reason
    assert "orb_options" in a.suspended and a.record_failures == 1


def test_management_record_failures_never_escape_the_candle_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fault injection on SYNTHETIC data: building every MANAGE record fails.
    The backtest completes, every trade still exits under its stops /
    targets / square-off, nothing is persisted for management, and RL is
    suspended for the strategy that traded."""
    from ml.inference_service import RLTradeAdvisor

    original = RLTradeAdvisor.decision_record
    suspended: dict[str, str] = {}

    def flaky(self: RLTradeAdvisor, **kw: Any) -> RLDecisionRecord:
        if kw.get("kind") is RLDecisionKind.MANAGE:
            raise ValueError("injected: invalid record")
        return original(self, **kw)

    real_suspend = RLTradeAdvisor.suspend

    def spy(self: RLTradeAdvisor, strategy_id: str, reason: str) -> None:
        suspended[strategy_id] = reason
        real_suspend(self, strategy_id, reason)

    monkeypatch.setattr(RLTradeAdvisor, "decision_record", flaky)
    monkeypatch.setattr(RLTradeAdvisor, "suspend", spy)

    @dataclass
    class EnterThenTrail(StubRL):
        def act(self, raw: np.ndarray, mask: np.ndarray) -> tuple[Action, dict[str, Any]]:
            if mask[Action.ENTER.value]:
                return Action.ENTER, {"confidence": 1.0}
            return (Action.TRAIL_STOP if mask[Action.TRAIL_STOP.value] else Action.HOLD), {"confidence": 0.8}

    ds = generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=2, seed=14,
                                                  strike_margin_points=200))
    sids = tuple(s for s, c in config().strategies.strategies.items() if c.enabled)
    models = {s: rl_stub(s, EnterThenTrail(Action.TRAIL_STOP)) for s in sids}
    res = asyncio.run(Backtester(config(), ds, BacktestOptions(rl_strategies=sids, rl_models=models,
                                                               rl_research=True)).run())
    assert res.trades and not res.observations.open_after_close
    assert all(t.status is TradeStatus.CLOSED and t.stop_loss >= t.initial_stop_loss for t in res.trades)
    assert not [d for d in res.rl_decisions if d.kind is RLDecisionKind.MANAGE]
    assert {t.strategy_id for t in res.trades} <= set(suspended)
    assert all("invalid" in r for r in suspended.values())
