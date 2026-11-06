"""Trading-mode and autonomy controller.

PAPER is the default and the only mode this build can run. A request to switch
to LIVE is evaluated against every precondition in ``docs/safety-policy.md``
and the reasons are reported — but it is refused regardless while
``live_available`` is False (no LIVE order routing exists), and always while
the configured Upstox token is a read-only analytics token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from core.clock import now_ist
from core.enums import AutonomyLevel, TradingMode, UpstoxTokenType

log = logging.getLogger(__name__)

LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


@dataclass(frozen=True)
class LivePreconditions:
    """Facts gathered from the running system at request time."""

    reconciliation_ok: bool
    kill_switch_armed: bool
    kill_switch_active: bool
    loss_limits_configured: bool
    paper_validation_passed: bool
    approved_by: str | None
    pending_orders: int
    broker_connected: bool


@dataclass(frozen=True)
class ModeChangeResult:
    approved: bool
    mode: TradingMode
    reasons: tuple[str, ...] = ()
    at: datetime = field(default_factory=now_ist)


class ModeController:
    def __init__(
        self,
        *,
        live_available: bool = False,
        autonomy: AutonomyLevel = AutonomyLevel.PAPER_AUTO_IMPROVE,
        token_type: UpstoxTokenType = UpstoxTokenType.ANALYTICS,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.mode = TradingMode.PAPER
        self.autonomy = autonomy
        self._live_available = live_available
        self._token_type = UpstoxTokenType(token_type)
        self._now = now_fn
        self.history: list[ModeChangeResult] = []

    def evaluate_live(self, confirmation: str, pre: LivePreconditions) -> list[str]:
        """Every unmet LIVE precondition, as human-readable reasons."""
        reasons: list[str] = []
        if confirmation != LIVE_CONFIRMATION_PHRASE:
            reasons.append(f"type exactly '{LIVE_CONFIRMATION_PHRASE}' to confirm")
        if not pre.broker_connected:
            reasons.append("broker not connected")
        if not pre.reconciliation_ok:
            reasons.append("broker reconciliation has not passed")
        if pre.pending_orders > 0:
            reasons.append(f"{pre.pending_orders} pending order(s): reconcile before changing mode")
        if not pre.kill_switch_armed:
            reasons.append("kill switch not armed")
        if pre.kill_switch_active:
            reasons.append("kill switch is active")
        if not pre.loss_limits_configured:
            reasons.append("daily loss limits not configured")
        if not pre.paper_validation_passed:
            reasons.append("PAPER validation not completed for this version")
        if not pre.approved_by:
            reasons.append("no named human approval for LIVE")
        if not self._live_available:
            reasons.append("LIVE order routing is not available in this build")
        if self._token_type is UpstoxTokenType.ANALYTICS:
            reasons.append("the Upstox Analytics Token is read-only and cannot place orders")
        return reasons

    def request_live(self, confirmation: str, pre: LivePreconditions) -> ModeChangeResult:
        reasons = self.evaluate_live(confirmation, pre)
        result = ModeChangeResult(approved=not reasons, mode=TradingMode.LIVE if not reasons else self.mode,
                                  reasons=tuple(reasons), at=self._now())
        if result.approved:  # pragma: no cover - unreachable while live_available is False
            self.mode = TradingMode.LIVE
        log.warning("LIVE mode request", extra={"approved": result.approved, "reasons": list(reasons)})
        self.history.append(result)
        return result

    def request_paper(self, pending_orders: int = 0) -> ModeChangeResult:
        if self.mode is TradingMode.PAPER:
            result = ModeChangeResult(True, TradingMode.PAPER, ("already PAPER",), self._now())
        elif pending_orders > 0:
            result = ModeChangeResult(False, self.mode, (f"{pending_orders} pending order(s)",), self._now())
        else:  # pragma: no cover - LIVE is unreachable in this build
            self.mode = TradingMode.PAPER
            result = ModeChangeResult(True, TradingMode.PAPER, (), self._now())
        self.history.append(result)
        return result

    def set_autonomy(self, level: AutonomyLevel) -> None:
        self.autonomy = AutonomyLevel(level)
        log.info("autonomy level set", extra={"autonomy": self.autonomy.value})
