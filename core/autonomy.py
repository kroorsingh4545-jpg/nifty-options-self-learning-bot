"""Autonomy policy: what the self-learning loop may do on its own.

One table, used by every promotion path (strategy versions in
``learning.promotion_gate`` and ML / RL models in ``ml.model_registry``) and
mirrored by triggers in ``data/storage/schema.sql``:

=========================  ===========  ==============  ===============  =============
level                      PAPER_READY  auto PAPER      LIVE_CANDIDATE   LIVE
                                        deployment      nomination
=========================  ===========  ==============  ===============  =============
OBSERVE_ONLY               no           no              no               never
PAPER_AUTO_IMPROVE         yes          yes             no               never
LIVE_CANDIDATE_APPROVAL    yes          yes             yes              never
=========================  ===========  ==============  ===============  =============

LIVE is never reachable by the loop at any level: LIVE_APPROVED needs a
named human approver and a typed confirmation, and even then nothing in
this build deploys to LIVE (there is no LIVE order routing).
"""

from __future__ import annotations

from core.enums import AutonomyLevel

#: Levels that allow a candidate (strategy version or model) to become PAPER_READY.
PAPER_PROMOTION_LEVELS = frozenset({AutonomyLevel.PAPER_AUTO_IMPROVE, AutonomyLevel.LIVE_CANDIDATE_APPROVAL})
#: Levels that allow a PAPER-validated version to be nominated LIVE_CANDIDATE.
LIVE_NOMINATION_LEVELS = frozenset({AutonomyLevel.LIVE_CANDIDATE_APPROVAL})

#: Approver names that are not a human. A LIVE approval (or a manual PAPER
#: promotion) naming one of these is refused.
NON_HUMAN_APPROVERS = frozenset({"", "auto", "automatic", "bot", "system", "learning_loop", "reflective_loop",
                                 "promotion_gate", "deployment_controller", "scheduler", "none", "null"})

#: The loop's own actor name in audit rows (it is never accepted as a human approver).
LOOP_ACTOR = "learning_loop"


class AutonomyError(PermissionError):
    """The current autonomy level does not allow this transition."""


def may_promote_to_paper(level: AutonomyLevel | str) -> bool:
    return AutonomyLevel(level) in PAPER_PROMOTION_LEVELS


def may_auto_deploy_paper(level: AutonomyLevel | str) -> bool:
    return AutonomyLevel(level) in PAPER_PROMOTION_LEVELS


def may_nominate_live_candidate(level: AutonomyLevel | str) -> bool:
    return AutonomyLevel(level) in LIVE_NOMINATION_LEVELS


def require_paper_promotion(level: AutonomyLevel | str) -> AutonomyLevel:
    lvl = AutonomyLevel(level)
    if not may_promote_to_paper(lvl):
        raise AutonomyError(f"autonomy level {lvl.value} does not allow promotion to PAPER_READY "
                            f"(needs one of {sorted(x.value for x in PAPER_PROMOTION_LEVELS)})")
    return lvl


def require_live_nomination(level: AutonomyLevel | str) -> AutonomyLevel:
    lvl = AutonomyLevel(level)
    if not may_nominate_live_candidate(lvl):
        raise AutonomyError(f"autonomy level {lvl.value} does not allow LIVE_CANDIDATE nominations "
                            f"(needs {AutonomyLevel.LIVE_CANDIDATE_APPROVAL.value})")
    return lvl


def is_human_approver(name: str | None) -> bool:
    return name is not None and name.strip().lower() not in NON_HUMAN_APPROVERS


#: Least to most permissive.
RESTRICTIVENESS: tuple[AutonomyLevel, ...] = (AutonomyLevel.OBSERVE_ONLY, AutonomyLevel.PAPER_AUTO_IMPROVE,
                                              AutonomyLevel.LIVE_CANDIDATE_APPROVAL)


def most_restrictive(*levels: AutonomyLevel | str) -> AutonomyLevel:
    if not levels:
        raise ValueError("most_restrictive needs at least one level")
    return min((AutonomyLevel(x) for x in levels), key=RESTRICTIVENESS.index)


__all__ = ["LIVE_NOMINATION_LEVELS", "LOOP_ACTOR", "NON_HUMAN_APPROVERS", "PAPER_PROMOTION_LEVELS", "RESTRICTIVENESS",
           "AutonomyError", "is_human_approver", "may_auto_deploy_paper", "may_nominate_live_candidate",
           "may_promote_to_paper", "most_restrictive", "require_live_nomination", "require_paper_promotion"]
