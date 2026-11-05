"""The Reflective Loop: LOG -> REFLECT -> HYPOTHESIZE -> MUTATE -> VERIFY ->
PROMOTE / REJECT, then PAPER deployment and LIVE_CANDIDATE nominations.

One ``ReflectiveLoop.run`` per strategy (strategies never share evidence,
candidates or deployments):

1. LOG          journal rows in the reflection window (``trade_journal``)
2. REFLECT      ``ReflectionEngine`` report
3. HYPOTHESIZE  ``HypothesisGenerator`` (at most N, whitelisted changes)
4. MUTATE       ``MutationEngine.propose`` on the PRODUCTION version (the
                deployed PAPER version, else the YAML config)
5. VERIFY       ``ValidationEngine.verify`` against production
6. PROMOTE      ``PromotionGate.decide``: PAPER_READY only on HISTORICAL
                evidence and an autonomy level that allows it
7. DEPLOY       the best PAPER_READY candidate (largest OOS expectancy) goes
                to PAPER when ``learning.auto_deploy_paper`` is on and the
                autonomy level allows it
8. NOMINATE     PAPER versions past the paper-validation period become
                LIVE_CANDIDATE at LIVE_CANDIDATE_APPROVAL. LIVE itself waits
                for a named human (``PromotionGate.approve_live``) and is never
                deployed by this build.

Every stage writes ``learning_events``. The autonomy level is read again at
each promotion step, so a change on the dashboard takes effect at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from config.settings import BotConfig
from core.autonomy import may_auto_deploy_paper
from core.clock import IST, now_ist
from core.enums import AutonomyLevel, LearningStage, StrategyVersionStatus
from core.ids import new_uid
from data.storage.records import LearningEventRecord, TradeJournalRecord
from data.storage.repositories import Repositories
from learning.deployment_controller import DeploymentController
from learning.hypothesis_generator import HypothesisGenerator
from learning.mutation_engine import MutationEngine, MutationRefused
from learning.promotion_gate import Outcome, PromotionGate
from learning.reflection_engine import ReflectionEngine, ReflectionReport, render_markdown
from learning.validation_engine import ValidationEngine, ValidationReport

log = logging.getLogger(__name__)


@dataclass
class CandidateOutcome:
    hypothesis: dict[str, Any]
    version: str | None
    version_uid: str | None
    outcome: str
    reasons: list[str]
    validation: dict[str, Any] | None = None


@dataclass
class StrategyRun:
    strategy_id: str
    production_version: str
    candidates: list[CandidateOutcome] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deployed: str | None = None
    nominated: list[str] = field(default_factory=list)


@dataclass
class LoopResult:
    run_uid: str
    autonomy: AutonomyLevel
    reflection: ReflectionReport
    strategies: dict[str, StrategyRun]
    evidence_source: str

    @property
    def synthetic(self) -> bool:
        return self.reflection.synthetic or self.evidence_source != "HISTORICAL"

    def to_dict(self) -> dict[str, Any]:
        return {"run_uid": self.run_uid, "autonomy": self.autonomy.value, "synthetic": self.synthetic,
                "evidence_source": self.evidence_source, "reflection": self.reflection.to_dict(),
                "strategies": {k: v.__dict__ | {"candidates": [c.__dict__ for c in v.candidates]}
                               for k, v in self.strategies.items()}}


class ReflectiveLoop:
    def __init__(self, cfg: BotConfig, repos: Repositories, *, validation: Callable[[BotConfig], ValidationEngine],
                 autonomy_fn: Callable[[], AutonomyLevel], now_fn: Callable[[], datetime] = now_ist) -> None:
        """``cfg`` is the YAML config; ``validation(production_cfg)`` builds the
        VERIFY engine over the verification dataset."""
        self.cfg = cfg
        self.repos = repos
        self.validation = validation
        self.autonomy_fn = autonomy_fn
        self.now_fn = now_fn
        self.gate = PromotionGate(repos, cfg, autonomy_fn, now_fn)
        self.deployments = DeploymentController(repos, cfg, autonomy_fn, now_fn)
        self.reflector = ReflectionEngine(now_fn=now_fn)

    def _event(self, sid: str, stage: LearningStage, outcome: str, payload: dict[str, Any], run_uid: str,
               base: str | None = None, cand: str | None = None) -> None:
        self.repos.learning_events.insert(LearningEventRecord(
            event_uid=new_uid("LE"), strategy_id=sid, stage=stage, base_version=base, candidate_version=cand,
            payload_json={"run": run_uid, **payload}, outcome=outcome, created_at=self.now_fn()))

    async def run(self, entries: Sequence[TradeJournalRecord], start: date, end: date, *,
                  strategy_ids: Sequence[str] | None = None) -> LoopResult:
        run_uid = new_uid("LOOP")
        sids = list(strategy_ids or [s for s, c in self.cfg.strategies.strategies.items() if c.enabled])
        production = self.deployments.effective_config()
        reflection = self.reflector.reflect(entries, start, end, strategy_ids=sids)
        out: dict[str, StrategyRun] = {}
        verifier: ValidationEngine | None = None
        for sid in sids:
            prod_scfg = production.strategies.strategies[sid]
            run = StrategyRun(sid, prod_scfg.version)
            out[sid] = run
            r = reflection.strategies[sid]
            window = [e for e in entries if e.strategy_id == sid
                      and start <= e.exit_time.astimezone(IST).date() <= end]
            self._event(sid, LearningStage.LOG, "LOGGED", {"trades": len(window), "evidence": r.evidence}, run_uid,
                        prod_scfg.version)
            self._event(sid, LearningStage.REFLECT, "SYNTHETIC" if r.synthetic else "REAL",
                        {"report": r.to_dict()}, run_uid, prod_scfg.version)
            gen = HypothesisGenerator(production).generate(
                r, confidences=[(e.ml_confidence, e.net_pnl) for e in window if e.ml_confidence is not None])
            run.observations, run.skipped = gen.observations, gen.skipped
            parent = self.gate.register_baseline(production, sid)
            engine = MutationEngine(production)
            passed: list[tuple[float, str, str]] = []
            for h in gen.hypotheses:
                self._event(sid, LearningStage.HYPOTHESIZE, "PROPOSED", {"hypothesis": h.as_dict()}, run_uid,
                            prod_scfg.version)
                try:
                    cand = engine.propose(sid, h.proposed_change,
                                          taken_versions=self.repos.strategy_versions.versions(sid))
                except MutationRefused as exc:
                    run.candidates.append(CandidateOutcome(h.as_dict(), None, None, "REFUSED", [str(exc)]))
                    self._event(sid, LearningStage.REJECT, "MUTATION_REFUSED", {"reason": str(exc)}, run_uid,
                                prod_scfg.version)
                    continue
                rec = self.gate.register_candidate(cand, h, parent)
                self._event(sid, LearningStage.MUTATE, "CANDIDATE", {"changes": [m.as_dict() for m in cand.mutations]},
                            run_uid, cand.base_version, cand.version)
                if verifier is None:
                    verifier = self.validation(production)
                report: ValidationReport = await verifier.verify(cand, h)
                self._event(sid, LearningStage.VERIFY, "PASSED" if report.passed else "FAILED",
                            {"report": report.to_dict()}, run_uid, cand.base_version, cand.version)
                decision = self.gate.decide(rec.version_uid, report, production)
                run.candidates.append(CandidateOutcome(h.as_dict(), cand.version, rec.version_uid,
                                                       decision.outcome.value, list(decision.reasons),
                                                       report.to_dict()))
                if decision.outcome is Outcome.PAPER_READY:
                    gain = report.summary["candidate_oos"]["expectancy"] or 0.0
                    passed.append((gain, rec.version_uid, cand.version))
            level = AutonomyLevel(self.autonomy_fn())
            if passed and self.cfg.settings.learning.auto_deploy_paper and may_auto_deploy_paper(level):
                _, uid, version = max(passed)
                self.deployments.deploy_paper(uid, reason=f"loop {run_uid}: best passing candidate")
                run.deployed = version
            active = self.deployments.active(sid)
            if active is not None and active.status is StrategyVersionStatus.PAPER_ACTIVE:
                d = self.gate.nominate_live_candidate(active.version_uid, self.repos.journal.list(sid))
                if d.outcome is Outcome.LIVE_CANDIDATE:
                    run.nominated.append(active.version)
        source = verifier.dataset.source if verifier is not None else "NOT RUN (no candidates)"
        return LoopResult(run_uid, AutonomyLevel(self.autonomy_fn()), reflection, out, source)


def render_loop_markdown(res: LoopResult) -> str:
    lines: list[str] = []
    if res.synthetic:
        lines += ["> **SYNTHETIC / RESEARCH RUN - generated data, NOT real NIFTY prices. Nothing from this run can "
                  "be promoted; candidates that pass stop at VERIFIED.**", ""]
    lines += [f"# Reflective Loop run {res.run_uid}", "", f"Autonomy: `{res.autonomy.value}`. Verification data: "
              f"`{res.evidence_source}`. LIVE always needs explicit human approval and is never deployed by "
              "this build.", ""]
    for sid, run in res.strategies.items():
        lines += [f"## {sid} (production {run.production_version})", ""]
        if not run.candidates:
            lines += ["No hypotheses this run.", ""]
        for c in run.candidates:
            h = c.hypothesis
            lines += [f"### {h['title']} -> {c.version or '-'}: **{c.outcome}**", "",
                      f"- Change: `{h['proposed_change']}` (was `{h['current_values']}`)",
                      f"- Evidence: {'; '.join(h['evidence'])}", f"- Expected effect: {h['expected_effect']}",
                      f"- Test metric: {h['test_metric']}, minimum sample {h['min_sample_size']} OOS trades",
                      f"- Risk impact: {h['risk_impact']}", f"- Rollback: {h['rollback_plan']}"]
            if c.validation:
                lines += ["", "| gate | result | detail |", "|---|---|---|"]
                lines += [f"| {g['gate']} | {'PASS' if g['passed'] else 'FAIL'} | {g['detail']} |"
                          for g in c.validation["gates"]]
            lines += ["", "Decision: " + "; ".join(c.reasons), ""]
        if run.observations:
            lines += ["Observations (need a logic change, not mutated):", *[f"- {o}" for o in run.observations], ""]
        lines += [f"Deployed to PAPER: {run.deployed or 'nothing'}. LIVE candidates nominated: "
                  f"{', '.join(run.nominated) or 'none'}.", ""]
    lines += ["---", "", render_markdown(res.reflection)]
    return "\n".join(lines)


__all__ = ["CandidateOutcome", "LoopResult", "ReflectiveLoop", "StrategyRun", "render_loop_markdown"]
