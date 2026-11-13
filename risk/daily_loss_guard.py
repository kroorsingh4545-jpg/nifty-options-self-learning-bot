"""Daily loss limits, trade caps, cooldowns and consecutive-loss pauses.

* Daily loss = -(realized + unrealized) — open losses count (mark-to-market).
* Per strategy: breach of ``max_daily_loss`` disables that strategy for the day
  and fires ``on_strategy_breach``.
* Global: breach of ``global.max_daily_loss`` (sum over strategies) fires
  ``on_global_breach`` exactly once (-> kill switch, flatten, disable all).
* ``max_trades_per_day``, ``loss_cooldown_minutes`` and
  ``max_consecutive_losses`` gate new entries per strategy.
* ``remaining_risk_budget`` caps per-trade risk so that a stop-out can never
  push a strategy or the portfolio past its daily limit. Given the open
  trades' stop-risk it assumes they are all stopped out too, so the open
  trades plus the new one together stay within the limits.
* State is persisted to ``daily_pnl`` and restored after a restart.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from config.settings import RiskLimits, StrategyRiskConfig
from core.clock import now_ist
from core.enums import TradingMode
from data.storage.records import DailyPnlRecord
from data.storage.repositories import DailyPnlRepository
from monitoring.json_logger import get_logger

log = get_logger(__name__)


@dataclass
class StrategyDay:
    strategy_id: str
    realized: float = 0.0
    unrealized: float = 0.0
    charges: float = 0.0
    trades: int = 0  # entries taken today
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    breached: bool = False
    paused_reason: str | None = None
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0

    @property
    def total(self) -> float:
        return self.realized + self.unrealized

    @property
    def loss(self) -> float:
        return max(0.0, -self.total)


class DailyLossGuard:
    def __init__(
        self,
        risk: RiskLimits,
        strategy_limits: Mapping[str, StrategyRiskConfig],
        *,
        mode: TradingMode = TradingMode.PAPER,
        repo: DailyPnlRepository | None = None,
        now_fn: Callable[[], datetime] = now_ist,
        on_strategy_breach: Callable[[str, float], None] | None = None,
        on_global_breach: Callable[[float], None] | None = None,
        on_pause: Callable[[str, str], None] | None = None,
    ) -> None:
        self.risk = risk
        self.limits = dict(strategy_limits)
        self.mode = mode
        self.repo = repo
        self._now = now_fn
        self.on_strategy_breach = on_strategy_breach
        self.on_global_breach = on_global_breach
        self.on_pause = on_pause
        self.day: date = now_fn().date()
        self.state: dict[str, StrategyDay] = {sid: StrategyDay(sid) for sid in self.limits}
        self.global_breached = False

    # ------------------------------------------------------------------ #
    def _s(self, sid: str) -> StrategyDay:
        if sid not in self.state:
            raise KeyError(f"unknown strategy {sid!r}")
        return self.state[sid]

    def _limit(self, sid: str) -> float:
        return min(self.limits[sid].max_daily_loss, self.risk.per_strategy.max_daily_loss)

    @property
    def global_loss(self) -> float:
        return max(0.0, -sum(s.total for s in self.state.values()))

    def roll_day_if_needed(self) -> None:
        today = self._now().date()
        if today != self.day:
            log.info("daily loss guard: new trading day", extra={"day": today.isoformat()})
            self.day = today
            self.state = {sid: StrategyDay(sid) for sid in self.limits}
            self.global_breached = False

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def record_entry(self, sid: str) -> None:
        self.roll_day_if_needed()
        self._s(sid).trades += 1
        self._persist(sid)

    def record_trade_close(self, sid: str, net_pnl: float, charges: float = 0.0,
                           unrealized_after: float | None = None) -> None:
        """``unrealized_after`` is the strategy's open P&L without this trade.
        It is applied together with the realized P&L, so the closed trade is
        never counted twice (once realized, once still marked open) when
        drawdown and the loss limits are evaluated."""
        self.roll_day_if_needed()
        s = self._s(sid)
        s.realized += net_pnl
        s.charges += charges
        if unrealized_after is not None:
            s.unrealized = unrealized_after
        if net_pnl < 0:
            s.losses += 1
            s.consecutive_losses += 1
            s.last_loss_at = self._now()
            if s.consecutive_losses >= min(self.limits[sid].max_consecutive_losses,
                                           self.risk.per_strategy.max_consecutive_losses):
                self._pause(s, f"{s.consecutive_losses} consecutive losses")
        else:
            s.wins += 1
            s.consecutive_losses = 0
        self._evaluate(sid)

    def update_unrealized(self, sid: str, unrealized: float) -> None:
        self.roll_day_if_needed()
        self._s(sid).unrealized = unrealized
        self._evaluate(sid)

    def block_entries_today(self, sid: str, reason: str) -> None:
        """Refuse this strategy's new entries until the next trading day because
        its market data cannot be trusted. Exits are untouched. Deliberately
        not persisted: a restart re-checks the data, and full data lifts it."""
        self.roll_day_if_needed()
        s = self._s(sid)
        if s.paused_reason is None:
            s.paused_reason = reason
            log.error("strategy entries blocked for the day", extra={"strategy_id": sid, "reason": reason})

    def _pause(self, s: StrategyDay, reason: str) -> None:
        if s.paused_reason is None:
            s.paused_reason = reason
            log.warning("strategy paused for the day", extra={"strategy_id": s.strategy_id, "reason": reason})
            if self.on_pause:
                self.on_pause(s.strategy_id, reason)

    def _evaluate(self, sid: str) -> None:
        s = self._s(sid)
        s.peak_pnl = max(s.peak_pnl, s.total)
        s.max_drawdown = max(s.max_drawdown, s.peak_pnl - s.total)
        if not s.breached and s.loss >= self._limit(sid) - 1e-9:
            s.breached = True
            log.critical("strategy daily loss limit breached",
                         extra={"strategy_id": sid, "loss": round(s.loss, 2), "limit": self._limit(sid)})
            if self.on_strategy_breach:
                self.on_strategy_breach(sid, s.loss)
        self._persist(sid)
        if not self.global_breached and self.global_loss >= self.risk.global_.max_daily_loss - 1e-9:
            self.global_breached = True
            log.critical("GLOBAL daily loss limit breached",
                         extra={"loss": round(self.global_loss, 2), "limit": self.risk.global_.max_daily_loss})
            if self.on_global_breach:
                self.on_global_breach(self.global_loss)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def can_enter(self, sid: str) -> tuple[bool, list[str]]:
        self.roll_day_if_needed()
        s = self._s(sid)
        lim = self.limits[sid]
        reasons: list[str] = []
        if self.global_breached:
            reasons.append("global daily loss limit breached")
        if s.breached:
            reasons.append("strategy daily loss limit breached")
        if s.paused_reason:
            reasons.append(f"strategy paused: {s.paused_reason}")
        max_trades = min(lim.max_trades_per_day, self.risk.per_strategy.max_trades_per_day)
        if s.trades >= max_trades:
            reasons.append(f"daily trade cap reached ({s.trades}/{max_trades})")
        cooldown = max(lim.loss_cooldown_minutes, self.risk.per_strategy.loss_cooldown_minutes)
        if s.last_loss_at is not None and self._now() - s.last_loss_at < timedelta(minutes=cooldown):
            left = timedelta(minutes=cooldown) - (self._now() - s.last_loss_at)
            reasons.append(f"cooldown after loss ({math.ceil(left.total_seconds() / 60)} min left)")
        return (not reasons, reasons)

    def remaining_risk_budget(self, sid: str, open_risk: Mapping[str, float] | None = None) -> float:
        """Largest loss a new trade may risk without breaching any daily limit.

        ``open_risk``: worst-case loss per strategy of its open trades if their
        stops are hit (``risk_manager.open_stop_risk``). It is counted as
        already lost (in place of their mark-to-market, whichever is worse),
        for this strategy and, summed over every strategy, globally."""
        s = self._s(sid)
        strategy_loss, global_loss = s.loss, self.global_loss
        if open_risk is not None:
            strategy_loss = max(strategy_loss, -s.realized + max(0.0, open_risk.get(sid, 0.0)))
            global_loss = max(global_loss, -sum(x.realized for x in self.state.values())
                              + sum(max(0.0, v) for v in open_risk.values()))
        strategy_left = self._limit(sid) - strategy_loss
        global_left = self.risk.global_.max_daily_loss - global_loss
        return max(0.0, min(self.risk.per_trade.max_risk_per_trade, strategy_left, global_left))

    def snapshot(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "global_loss": round(self.global_loss, 2),
            "global_limit": self.risk.global_.max_daily_loss,
            "global_breached": self.global_breached,
            "strategies": {
                sid: {"realized": round(s.realized, 2), "unrealized": round(s.unrealized, 2),
                      "loss": round(s.loss, 2), "limit": self._limit(sid), "trades": s.trades,
                      "consecutive_losses": s.consecutive_losses, "breached": s.breached,
                      "paused": s.paused_reason}
                for sid, s in self.state.items()
            },
        }

    # ------------------------------------------------------------------ #
    # Persistence / restart recovery
    # ------------------------------------------------------------------ #
    def _persist(self, sid: str) -> None:
        if self.repo is None:
            return
        s = self.state[sid]
        self.repo.upsert(DailyPnlRecord(
            trade_date=self.day, mode=self.mode, strategy_id=sid, realized_pnl=round(s.realized, 2),
            unrealized_pnl=round(s.unrealized, 2), charges=round(s.charges, 2),
            trades_count=max(s.trades, s.wins + s.losses), wins=s.wins, losses=s.losses,
            consecutive_losses=s.consecutive_losses, max_drawdown=round(s.max_drawdown, 2),
            loss_limit_breached=s.breached,
        ))

    def restore(self) -> None:
        """Reload today's state after a restart (counts, losses, breaches).

        The cooldown clock restarts conservatively from the restore time when
        the last recorded trade of the day was a loss.
        """
        if self.repo is None:
            return
        for rec in self.repo.list_for_day(self.day):
            if rec.strategy_id not in self.state:
                continue
            s = self.state[rec.strategy_id]
            s.realized, s.unrealized, s.charges = rec.realized_pnl, rec.unrealized_pnl, rec.charges
            s.trades, s.wins, s.losses = rec.trades_count, rec.wins, rec.losses
            s.consecutive_losses, s.max_drawdown, s.breached = rec.consecutive_losses, rec.max_drawdown, \
                rec.loss_limit_breached
            if s.consecutive_losses > 0:
                s.last_loss_at = self._now()
            if s.consecutive_losses >= self.risk.per_strategy.max_consecutive_losses:
                s.paused_reason = f"{s.consecutive_losses} consecutive losses (restored)"
        if self.global_loss >= self.risk.global_.max_daily_loss:
            self.global_breached = True
        log.info("daily loss guard restored", extra=self.snapshot())
