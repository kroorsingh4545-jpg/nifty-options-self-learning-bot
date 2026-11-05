"""PROMOTE / REJECT stage of the Reflective Loop.

``PromotionGate.decide`` takes a candidate version and its VERIFY report:

* any gate failed -> REJECTED, archived with the full evaluation
* all gates passed -> VERIFIED, then PAPER_READY only if ALL of:
  - the evidence is HISTORICAL (SYNTHETIC / UNKNOWN stops at VERIFIED:
    research only, never promoted; the schema refuses it too)
  - the autonomy level read AT THIS MOMENT allows PAPER promotion
    (OBSERVE_ONLY holds it at VERIFIED)
  - the stored config still differs from the YAML config only in
    whitelisted leaves and passes every validator
  - it was built on the version that is in production now
* ``nominate_live_candidate``: a PAPER_ACTIVE version becomes LIVE_CANDIDATE
  only at autonomy LIVE_CANDIDATE_APPROVAL, after the configured
  paper-validation period on the real feed (LIVE_FEED journal rows of that
  version: enough days and trades, positive net and expectancy)
* ``approve_live``: LIVE_APPROVED needs a named HUMAN approver and the typed
  phrase ``APPROVE LIVE <strategy> <version>``. It records the approval only:
  nothing in this build deploys to LIVE.

``promote_model_to_paper`` is the ML / RL model path: it reads the live
autonomy level and calls ``ModelRegistry.mark_paper_ready`` (Phase 7 audit
warning 5). Every decision writes a ``learning_events`` row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from config.settings import BotConfig
from core.autonomy import (
    LOOP_ACTOR,
    AutonomyError,
    is_human_approver,
    may_nominate_live_candidate,
    may_promote_to_paper,
)
from core.clock import now_ist
from core.enums import AutonomyLevel, EvidenceSource, LearningStage, StrategyVersionStatus
from core.ids import new_uid
from data.storage.records import LearningEventRecord, StrategyVersionRecord, TradeJournalRecord
from data.storage.repositories import Repositories
from learning.hypothesis_generator import Hypothesis
from learning.mutation_engine import Candidate, MutationEngine, MutationRefused, dump_strategy
from learning.validation_engine import ValidationReport
from ml.model_registry import ModelRegistry

log = logging.getLogger(__name__)

AutonomyFn = Callable[[], AutonomyLevel]


class PromotionError(RuntimeError):
    pass


class Outcome(StrEnum):
    PAPER_READY = "PAPER_READY"
    VERIFIED_HELD = "VERIFIED_HELD"  # passed, but not promotable now (synthetic evidence / autonomy / stale)
    REJECTED = "REJECTED"
    LIVE_CANDIDATE = "LIVE_CANDIDATE"
    NOT_NOMINATED = "NOT_NOMINATED"
    LIVE_APPROVED = "LIVE_APPROVED"


@dataclass(frozen=True)
class PromotionDecision:
    version_uid: str
    strategy_id: str
    version: str
    outcome: Outcome
    reasons: tuple[str, ...]
    autonomy: AutonomyLevel


def live_confirmation_phrase(strategy_id: str, version: str) -> str:
    return f"APPROVE LIVE {strategy_id} {version}"


@dataclass
class PromotionGate:
    repos: Repositories
    cfg: BotConfig  # the YAML config (whitelist overlays are checked against it)
    autonomy_fn: AutonomyFn
    now_fn: Callable[[], datetime] = now_ist
    decisions: list[PromotionDecision] = field(default_factory=list)

    # Versions ----------------------------------------------------------------
    def register_baseline(self, production: BotConfig, strategy_id: str) -> StrategyVersionRecord:
        """The production version's row (the YAML baseline the first time)."""
        scfg = production.strategies.strategies[strategy_id]
        existing = self.repos.strategy_versions.find(strategy_id, scfg.version)
        if existing is not None:
            return existing
        rec = StrategyVersionRecord(version_uid=new_uid("SV"), strategy_id=strategy_id, version=scfg.version,
                                    status=StrategyVersionStatus.BASELINE, config_json=dump_strategy(scfg),
                                    status_reason="YAML configuration")
        self.repos.strategy_versions.insert(rec)
        got = self.repos.strategy_versions.get(rec.version_uid)
        assert got is not None
        return got

    def register_candidate(self, candidate: Candidate, hypothesis: Hypothesis | None,
                           parent: StrategyVersionRecord) -> StrategyVersionRecord:
        if parent.version != candidate.base_version or parent.strategy_id != candidate.strategy_id:
            raise PromotionError("the candidate's parent is not its base version")
        rec = StrategyVersionRecord(
            version_uid=new_uid("SV"), strategy_id=candidate.strategy_id, version=candidate.version,
            base_version=candidate.base_version, parent_uid=parent.version_uid, status=StrategyVersionStatus.CANDIDATE,
            config_json=candidate.config_json, mutation_json=[m.as_dict() for m in candidate.mutations],
            hypothesis_json=hypothesis.as_dict() if hypothesis else {}, status_reason="whitelisted mutation")
        self.repos.strategy_versions.insert(rec)
        got = self.repos.strategy_versions.get(rec.version_uid)
        assert got is not None
        return got

    # Events ------------------------------------------------------------------
    def event(self, stage: LearningStage, rec: StrategyVersionRecord, outcome: str, payload: dict[str, Any],
              approved_by: str | None = None) -> None:
        self.repos.learning_events.insert(LearningEventRecord(
            event_uid=new_uid("LE"), strategy_id=rec.strategy_id, stage=stage, base_version=rec.base_version,
            candidate_version=rec.version, payload_json=payload, outcome=outcome, approved_by=approved_by,
            created_at=self.now_fn()))

    def _decided(self, rec: StrategyVersionRecord, outcome: Outcome, reasons: Sequence[str],
                 level: AutonomyLevel) -> PromotionDecision:
        d = PromotionDecision(rec.version_uid, rec.strategy_id, rec.version, outcome, tuple(reasons), level)
        self.decisions.append(d)
        log.info("promotion decision", extra={"strategy_id": rec.strategy_id, "version": rec.version,
                                              "outcome": outcome.value, "reasons": list(reasons)})
        return d

    def _require(self, version_uid: str) -> StrategyVersionRecord:
        rec = self.repos.strategy_versions.get(version_uid)
        if rec is None:
            raise PromotionError(f"strategy version {version_uid!r} not found")
        return rec

    # PROMOTE / REJECT --------------------------------------------------------
    def decide(self, version_uid: str, report: ValidationReport, production: BotConfig) -> PromotionDecision:
        rec = self._require(version_uid)
        level = AutonomyLevel(self.autonomy_fn())
        if rec.status is not StrategyVersionStatus.CANDIDATE:
            raise PromotionError(f"only CANDIDATE versions are decided (is {rec.status.value})")
        if (report.strategy_id, report.candidate_version) != (rec.strategy_id, rec.version):
            raise PromotionError("the report is for another version")
        evaluation = report.to_dict()
        if not report.passed:
            reasons = [f"{g.gate}: {g.detail}" for g in report.gates if not g.passed]
            self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.REJECTED,
                                                    reason="; ".join(reasons)[:2000], evaluation=evaluation,
                                                    evidence_source=report.evidence_source)
            self.event(LearningStage.REJECT, rec, "REJECTED", {"failed_gates": report.failed_gates,
                                                               "evaluation": evaluation})
            return self._decided(rec, Outcome.REJECTED, reasons, level)
        self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.VERIFIED,
                                                reason="every VERIFY gate passed", evaluation=evaluation,
                                                evidence_source=report.evidence_source)
        held: list[str] = []
        if report.evidence_source is not EvidenceSource.HISTORICAL:
            held.append(f"verified on {report.evidence_source.value} data: research only, never promoted")
        if not may_promote_to_paper(level):
            held.append(f"autonomy level {level.value} does not allow PAPER promotion")
        prod = production.strategies.strategies[rec.strategy_id]
        if prod.version != rec.base_version:
            held.append(f"built on {rec.base_version}, production is now {prod.version}")
        try:
            MutationEngine(self.cfg).apply_overlay(rec.strategy_id, rec.config_json)
        except MutationRefused as exc:
            held.append(f"no longer a valid whitelisted overlay of the YAML config: {exc}")
        if held:
            self.event(LearningStage.VERIFY, rec, "VERIFIED_HELD", {"reasons": held, "evaluation": evaluation,
                                                                    "autonomy": level.value})
            return self._decided(rec, Outcome.VERIFIED_HELD, held, level)
        self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.PAPER_READY, autonomy=level,
                                                reason=f"every gate passed on HISTORICAL data; autonomy {level.value}")
        self.event(LearningStage.PROMOTE, rec, "PAPER_READY", {"autonomy": level.value, "evaluation": evaluation,
                                                               "actor": LOOP_ACTOR})
        return self._decided(rec, Outcome.PAPER_READY, ["every gate passed on HISTORICAL data"], level)

    # LIVE_CANDIDATE ----------------------------------------------------------
    def paper_evidence(self, rec: StrategyVersionRecord, entries: Sequence[TradeJournalRecord]) -> dict[str, Any]:
        mine = [e for e in entries if e.strategy_id == rec.strategy_id and e.strategy_version == rec.version
                and e.data_source is EvidenceSource.LIVE_FEED]
        nets = [e.net_pnl for e in mine]
        return {"trades": len(mine), "days": len({e.exit_time.date() for e in mine}),
                "net_pnl": round(sum(nets), 2), "expectancy": round(sum(nets) / len(nets), 2) if nets else None,
                "errors": sum(1 for e in mine if e.errors_json)}

    def nominate_live_candidate(self, version_uid: str, entries: Sequence[TradeJournalRecord]) -> PromotionDecision:
        rec = self._require(version_uid)
        level = AutonomyLevel(self.autonomy_fn())
        lc = self.cfg.settings.learning
        ev = self.paper_evidence(rec, entries)
        reasons: list[str] = []
        if rec.status is not StrategyVersionStatus.PAPER_ACTIVE:
            reasons.append(f"only a version deployed in PAPER can be nominated (is {rec.status.value})")
        if not may_nominate_live_candidate(level):
            reasons.append(f"autonomy level {level.value} does not allow LIVE_CANDIDATE nominations")
        if ev["days"] < lc.paper_validation_days:
            reasons.append(f"{ev['days']} PAPER days on the real feed (need {lc.paper_validation_days})")
        if ev["trades"] < lc.paper_validation_min_trades:
            reasons.append(f"{ev['trades']} PAPER trades on the real feed (need {lc.paper_validation_min_trades})")
        if ev["trades"] and (ev["net_pnl"] <= 0 or (ev["expectancy"] or 0) <= 0):
            reasons.append(f"PAPER net {ev['net_pnl']} / expectancy {ev['expectancy']} is not positive")
        if reasons:
            return self._decided(rec, Outcome.NOT_NOMINATED, reasons, level)
        self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.LIVE_CANDIDATE, autonomy=level,
                                                reason=f"PAPER validation passed: {ev}")
        self.event(LearningStage.PROMOTE, rec, "LIVE_CANDIDATE", {"paper": ev, "autonomy": level.value,
                                                                  "note": "waiting for explicit human approval"})
        return self._decided(rec, Outcome.LIVE_CANDIDATE, ["PAPER validation passed; waiting for human approval"],
                             level)

    # LIVE approval (human only) ----------------------------------------------
    def approve_live(self, version_uid: str, *, approved_by: str, confirmation: str) -> PromotionDecision:
        rec = self._require(version_uid)
        level = AutonomyLevel(self.autonomy_fn())
        if rec.status is not StrategyVersionStatus.LIVE_CANDIDATE:
            raise PromotionError(f"only LIVE_CANDIDATE versions can be approved for LIVE (is {rec.status.value})")
        if not is_human_approver(approved_by):
            raise AutonomyError("LIVE approval needs a named human; the loop can never approve LIVE")
        want = live_confirmation_phrase(rec.strategy_id, rec.version)
        if confirmation != want:
            raise PromotionError(f"type exactly {want!r} to approve")
        self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.LIVE_APPROVED,
                                                approved_by=approved_by.strip(),
                                                reason="human LIVE approval recorded; this build has no LIVE "
                                                       "deployment path")
        self.event(LearningStage.PROMOTE, rec, "LIVE_APPROVED", {"note": "recorded only: no LIVE deployment in this "
                                                                         "build"}, approved_by=approved_by.strip())
        return self._decided(rec, Outcome.LIVE_APPROVED, ["human approval recorded (not deployed)"], level)


def promote_model_to_paper(registry: ModelRegistry, run_id: str, *, approved_by: str,
                           autonomy_fn: AutonomyFn) -> None:
    """VALIDATED model -> PAPER_READY at the autonomy level in force NOW."""
    registry.mark_paper_ready(run_id, approved_by=approved_by, autonomy=AutonomyLevel(autonomy_fn()))


__all__ = ["AutonomyFn", "Outcome", "PromotionDecision", "PromotionError", "PromotionGate",
           "live_confirmation_phrase", "promote_model_to_paper"]
