"""Deployment controller: which strategy version runs in PAPER.

* ``deploy_paper``: a PAPER_READY version (HISTORICAL evidence) becomes the
  PAPER version of its strategy, if the autonomy level read now allows it.
  The version it replaces becomes SUPERSEDED and stays ready to restore.
* ``rollback``: back to the version that was in PAPER before (or the YAML
  baseline when there was none, or when autonomy no longer allows any
  learned version). The rolled-back version is ROLLED_BACK. Always allowed.
* ``retire``: back to the YAML baseline. Always allowed.
* ``effective_config``: the YAML config with every strategy's PAPER version
  overlaid (re-checked: only whitelisted leaves may differ from the YAML, and
  every validator must pass; a version that fails falls back to the YAML
  config with a CRITICAL log). The bot's composition root applies it at
  start-up; a deployment takes effect at the next bot start, never inside a
  running session.
* ``deploy_live``: always refused. This build has no LIVE deployment path,
  and the schema cannot even record one (strategy_deployments is PAPER-only).

Every change is a row in ``strategy_deployments`` (append-only) plus a
``learning_events`` row: who, when, why, what was replaced, and the
autonomy level.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from config.settings import BotConfig
from core.autonomy import LOOP_ACTOR, may_auto_deploy_paper
from core.clock import now_ist
from core.enums import AutonomyLevel, DeploymentAction, LearningStage, StrategyVersionStatus, TradingMode
from core.ids import new_uid
from data.storage.records import LearningEventRecord, StrategyDeploymentRecord, StrategyVersionRecord
from data.storage.repositories import Repositories
from learning.mutation_engine import MutationEngine, MutationRefused

log = logging.getLogger(__name__)


class DeploymentError(RuntimeError):
    pass


class LiveDeploymentRefused(DeploymentError):
    pass


IN_PAPER = (StrategyVersionStatus.PAPER_ACTIVE, StrategyVersionStatus.LIVE_CANDIDATE,
            StrategyVersionStatus.LIVE_APPROVED)


@dataclass
class DeploymentController:
    repos: Repositories
    cfg: BotConfig  # the YAML config
    autonomy_fn: Callable[[], AutonomyLevel]
    now_fn: Callable[[], datetime] = now_ist

    def __post_init__(self) -> None:
        if self.cfg.mode is not TradingMode.PAPER or self.repos.mode is not TradingMode.PAPER:
            raise LiveDeploymentRefused("the deployment controller only runs in PAPER mode")

    # Queries -----------------------------------------------------------------
    def active(self, strategy_id: str) -> StrategyVersionRecord | None:
        cur = self.repos.deployments.current(strategy_id)
        if cur is None or cur.version_uid is None:
            return None
        return self.repos.strategy_versions.get(cur.version_uid)

    def effective_config(self) -> BotConfig:
        cfg = self.cfg
        for sid in self.cfg.strategies.strategies:
            rec = self.active(sid)
            if rec is None:
                continue
            if rec.status not in IN_PAPER:
                log.critical("deployed strategy version is not in a PAPER state; using the YAML config",
                             extra={"strategy_id": sid, "version": rec.version, "status": rec.status.value})
                continue
            try:
                cfg = MutationEngine(cfg).apply_overlay(sid, rec.config_json)
            except MutationRefused as exc:
                log.critical("deployed strategy version no longer fits the YAML config; using the YAML config",
                             extra={"strategy_id": sid, "version": rec.version, "reason": str(exc)})
        return cfg

    # Changes -----------------------------------------------------------------
    def _event(self, rec: StrategyVersionRecord | None, sid: str, outcome: str, payload: dict[str, object],
               actor: str) -> None:
        self.repos.learning_events.insert(LearningEventRecord(
            event_uid=new_uid("LE"), strategy_id=sid, stage=LearningStage.PROMOTE,
            base_version=rec.base_version if rec else None, candidate_version=rec.version if rec else None,
            payload_json={**payload, "actor": actor}, outcome=outcome, created_at=self.now_fn()))

    def _record(self, sid: str, action: DeploymentAction, version_uid: str | None, previous: str | None,
                actor: str, level: AutonomyLevel, reason: str) -> None:
        self.repos.deployments.insert(StrategyDeploymentRecord(
            deployment_uid=new_uid("DEP"), strategy_id=sid, action=action, version_uid=version_uid,
            previous_version_uid=previous, actor=actor, autonomy_level=level, reason=reason[:500],
            created_at=self.now_fn()))

    def deploy_paper(self, version_uid: str, *, actor: str = LOOP_ACTOR, reason: str = "") -> StrategyVersionRecord:
        level = AutonomyLevel(self.autonomy_fn())
        rec = self.repos.strategy_versions.get(version_uid)
        if rec is None:
            raise DeploymentError(f"strategy version {version_uid!r} not found")
        if rec.status is not StrategyVersionStatus.PAPER_READY:
            raise DeploymentError(f"only PAPER_READY versions deploy (is {rec.status.value})")
        if not may_auto_deploy_paper(level):
            raise DeploymentError(f"autonomy level {level.value} does not allow PAPER deployment")
        MutationEngine(self.cfg).apply_overlay(rec.strategy_id, rec.config_json)  # still a valid overlay
        prev = self.active(rec.strategy_id)
        with self.repos.db.transaction():
            self._record(rec.strategy_id, DeploymentAction.DEPLOY, version_uid, prev.version_uid if prev else None,
                         actor, level, reason or "promotion gate passed")
            if prev is not None and prev.status in IN_PAPER:
                self.repos.strategy_versions.set_status(prev.version_uid, StrategyVersionStatus.SUPERSEDED,
                                                        reason=f"replaced in PAPER by {rec.version}")
            self.repos.strategy_versions.set_status(version_uid, StrategyVersionStatus.PAPER_ACTIVE, autonomy=level,
                                                    reason=f"deployed to PAPER by {actor}")
        self._event(rec, rec.strategy_id, "DEPLOYED_PAPER", {"replaced": prev.version if prev else "YAML baseline",
                                                              "autonomy": level.value,
                                                              "rollback_to": prev.version if prev else "YAML"}, actor)
        got = self.repos.strategy_versions.get(version_uid)
        assert got is not None
        return got

    def rollback(self, strategy_id: str, *, actor: str, reason: str) -> StrategyVersionRecord | None:
        """Restore the previous PAPER version (None = the YAML baseline)."""
        level = AutonomyLevel(self.autonomy_fn())
        cur_dep = self.repos.deployments.current(strategy_id)
        cur = self.active(strategy_id)
        if cur is None:
            raise DeploymentError(f"{strategy_id} already runs the YAML baseline")
        prev = self.repos.strategy_versions.get(cur_dep.previous_version_uid) if cur_dep and \
            cur_dep.previous_version_uid else None
        restore = prev if prev is not None and prev.status is StrategyVersionStatus.SUPERSEDED \
            and may_auto_deploy_paper(level) else None
        with self.repos.db.transaction():
            if restore is not None:
                self._record(strategy_id, DeploymentAction.ROLLBACK, restore.version_uid, cur.version_uid, actor,
                             level, reason)
                self.repos.strategy_versions.set_status(restore.version_uid, StrategyVersionStatus.PAPER_ACTIVE,
                                                        autonomy=level, reason=f"restored by rollback: {reason}")
            else:
                self._record(strategy_id, DeploymentAction.RETIRE, None, cur.version_uid, actor, level, reason)
            self.repos.strategy_versions.set_status(cur.version_uid, StrategyVersionStatus.ROLLED_BACK,
                                                    reason=f"rolled back by {actor}: {reason}")
        self._event(cur, strategy_id, "ROLLED_BACK", {"restored": restore.version if restore else "YAML baseline",
                                                       "reason": reason}, actor)
        return self.repos.strategy_versions.get(restore.version_uid) if restore else None

    def retire(self, strategy_id: str, *, actor: str, reason: str) -> None:
        """Back to the YAML baseline (the current version is SUPERSEDED, restorable)."""
        level = AutonomyLevel(self.autonomy_fn())
        cur = self.active(strategy_id)
        if cur is None:
            return
        with self.repos.db.transaction():
            self._record(strategy_id, DeploymentAction.RETIRE, None, cur.version_uid, actor, level, reason)
            if cur.status in IN_PAPER:
                self.repos.strategy_versions.set_status(cur.version_uid, StrategyVersionStatus.SUPERSEDED,
                                                        reason=f"retired to the YAML baseline: {reason}")
        self._event(cur, strategy_id, "RETIRED", {"reason": reason}, actor)

    def deploy_live(self, *_: object, **__: object) -> None:
        raise LiveDeploymentRefused("LIVE deployment does not exist in this build: LIVE needs explicit human "
                                    "approval, LIVE order routing, and the LIVE safety checklist")


__all__ = ["DeploymentController", "DeploymentError", "LiveDeploymentRefused"]
