"""Pre-trade risk gate and kill-switch trigger monitors.

``RiskManager.evaluate_entry`` is the ONLY way an entry becomes an order. In
order, it checks:

 1. kill switch (global / strategy)
 2. strategy enabled & active
 3. session: an NSE trading day (weekday, not a configured holiday), the
    holiday calendar loaded for the current year (otherwise every entry is
    blocked), market open, strategy entry window, no entries after
    ``no_new_entries_after`` (14:45), never inside the square-off window
 4. daily guard: loss limits, trade cap, cooldown, consecutive-loss pause
 5. open-trade caps: per strategy (2) and total (4), pending entries included
 6. fresh live quote for the contract (no stale / snapshot data)
 7. bid-ask spread within the strategy liquidity limit
 8. stop / target levels (stop always present)
 9. risk budget = min(max risk per trade, strategy sizing, remaining daily
    strategy and global loss budget) x ML size multiplier (only <= 1)
10. position size from budget, premium, stop distance and margin

Monitors translate infrastructure failures into kill-switch triggers:
WebSocket down beyond the grace period (or FAILED / AUTH_FAILED), auth
failure, rate-limit circuit lock, repeated API failures, abnormal spread on a
held contract.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from config.settings import BotConfig, StrategyConfig, TrailingConfig
from core.clock import now_ist
from core.enums import OptionType, StrategyStatus
from core.models import Instrument, Tick
from monitoring.json_logger import get_logger
from risk.daily_loss_guard import DailyLossGuard
from risk.kill_switch import KillReason, KillSwitch
from risk.position_sizer import CostFn, SizingResult, size_position
from risk.stop_loss_manager import ProtectiveLevels, compute_levels

log = get_logger(__name__)


@dataclass(frozen=True)
class EntryProposal:
    strategy_id: str
    instrument: Instrument
    option_type: OptionType
    reason: str
    size_multiplier: float = 1.0  # from ML filter; clipped to (0, 1]
    ml_confidence: float | None = None
    regime: str | None = None
    underlying_price: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    proposal: EntryProposal
    entry_price: float | None = None
    levels: ProtectiveLevels | None = None
    sizing: SizingResult | None = None
    risk_budget: float = 0.0
    trailing: TrailingConfig | None = None
    checked_at: datetime = field(default_factory=now_ist)

    @property
    def quantity(self) -> int:
        return self.sizing.quantity if self.sizing and self.approved else 0


class OpenTradeCounter:
    """Protocol-like base: implemented by execution.position_manager."""

    def open_trade_count(self, strategy_id: str | None = None) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class RiskManager:
    def __init__(
        self,
        cfg: BotConfig,
        *,
        guard: DailyLossGuard,
        kill_switch: KillSwitch,
        trades: OpenTradeCounter,
        quote_fn: Callable[[str], Tick | None],
        is_fresh_fn: Callable[[str], bool],
        strategy_status_fn: Callable[[str], StrategyStatus],
        cost_fn: CostFn | None = None,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self.cfg = cfg
        self.guard = guard
        self.kill_switch = kill_switch
        self.trades = trades
        self._quote = quote_fn
        self._fresh = is_fresh_fn
        self._status = strategy_status_fn
        self._cost_fn = cost_fn
        self._now = now_fn
        ks = cfg.risk.kill_switch
        self._spread_strikes: dict[str, int] = {}
        self._ks_cfg = ks
        self._api_failures = 0
        self._ws_timer: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # Session time
    # ------------------------------------------------------------------ #
    def entries_allowed_at(self, at: datetime, strategy: StrategyConfig) -> list[str]:
        rules = self.cfg.risk.time_rules
        session = self.cfg.settings.market_session
        reasons = []
        t = at.time()
        if not session.holidays_known_for(at.year):
            reasons.append(f"no NSE holiday calendar configured for {at.year}: new entries blocked")
        elif not session.is_trading_day(at.date()):
            reasons.append(f"{at.date()} is not an NSE trading day")
        if not session.market_open <= t < session.market_close:
            reasons.append("market closed")
        if t >= rules.no_new_entries_after:
            reasons.append(f"no new entries after {rules.no_new_entries_after:%H:%M}")
        if rules.square_off_start <= t:
            reasons.append("square-off window")
        if not strategy.entry_window.start <= t < strategy.entry_window.end:
            w = strategy.entry_window
            reasons.append(f"outside entry window {w.start:%H:%M}-{w.end:%H:%M}")
        return reasons

    # ------------------------------------------------------------------ #
    # Entry evaluation
    # ------------------------------------------------------------------ #
    def evaluate_entry(self, p: EntryProposal, *, available_margin: float) -> RiskDecision:
        sid = p.strategy_id
        strategies = self.cfg.strategies.strategies
        if sid not in strategies:
            return self._reject(p, [f"unknown strategy {sid!r}"])
        scfg = strategies[sid]
        reasons: list[str] = []

        block = self.kill_switch.block_reason(sid)
        if block:
            reasons.append(block)
        if not scfg.enabled:
            reasons.append("strategy disabled in config")
        status = self._status(sid)
        if status is not StrategyStatus.ACTIVE:
            reasons.append(f"strategy status {status.value}")
        now = self._now()
        reasons += self.entries_allowed_at(now, scfg)
        ok, guard_reasons = self.guard.can_enter(sid)
        reasons += guard_reasons
        per_strategy_cap = min(scfg.risk.max_open_trades, self.cfg.risk.per_strategy.max_open_trades)
        if self.trades.open_trade_count(sid) >= per_strategy_cap:
            reasons.append(f"strategy open-trade cap reached ({per_strategy_cap})")
        if self.trades.open_trade_count(None) >= self.cfg.risk.global_.max_open_trades:
            reasons.append(f"total open-trade cap reached ({self.cfg.risk.global_.max_open_trades})")
        if p.instrument.option_type is not p.option_type:
            reasons.append("instrument option type does not match proposal")
        if reasons:
            return self._reject(p, reasons)

        key = p.instrument.instrument_key
        tick = self._quote(key)
        if tick is None or not self._fresh(key):
            return self._reject(p, ["no fresh live quote for contract"])
        if tick.ask is None or tick.bid is None or tick.spread is None:
            return self._reject(p, ["no two-sided quote"])
        mid = tick.mid or tick.ltp
        spread_pct = 100 * tick.spread / mid if mid else float("inf")
        max_spread = scfg.strike_selection.liquidity.max_spread_pct
        if spread_pct > max_spread:
            return self._reject(p, [f"spread {spread_pct:.2f}% exceeds {max_spread}%"])
        entry = tick.ask
        if not scfg.strike_selection.min_premium <= entry <= scfg.strike_selection.max_premium:
            return self._reject(p, [f"premium {entry} outside {scfg.strike_selection.min_premium}-"
                                    f"{scfg.strike_selection.max_premium}"])

        try:
            levels = compute_levels(entry, scfg.stop_loss, scfg.take_profit, p.instrument.tick_size)
        except ValueError as exc:
            return self._reject(p, [f"invalid protective levels: {exc}"])

        multiplier = min(1.0, max(0.0, p.size_multiplier)) * scfg.sizing.size_multiplier
        if multiplier <= 0:
            return self._reject(p, ["size multiplier is zero (ML veto)"])
        budget = min(scfg.sizing.risk_per_trade, self.guard.remaining_risk_budget(sid))
        exit_slip = self.cfg.settings.paper_broker.slippage_ticks * p.instrument.tick_size
        sizing = size_position(
            entry_price=entry, stop_price=levels.stop, lot_size=p.instrument.lot_size, risk_budget=budget,
            available_margin=min(available_margin, scfg.allocated_capital), size_multiplier=multiplier,
            exit_slippage=exit_slip, freeze_quantity=p.instrument.freeze_quantity, cost_fn=self._cost_fn,
        )
        if not sizing.ok:
            return self._reject(p, [f"position size zero: {sizing.reason}"], entry, levels, sizing, budget)
        log.info("entry approved by risk", extra={
            "strategy_id": sid, "instrument": key, "qty": sizing.quantity, "entry": entry, "stop": levels.stop,
            "target": levels.target, "risk": sizing.risk_amount, "budget": round(budget, 2)})
        return RiskDecision(True, (), p, entry, levels, sizing, budget, scfg.trailing)

    def _reject(self, p: EntryProposal, reasons: list[str], entry: float | None = None,
                levels: ProtectiveLevels | None = None, sizing: SizingResult | None = None,
                budget: float = 0.0) -> RiskDecision:
        log.info("entry rejected by risk", extra={"strategy_id": p.strategy_id, "reasons": reasons})
        return RiskDecision(False, tuple(reasons), p, entry, levels, sizing, budget)

    # ------------------------------------------------------------------ #
    # Kill-switch monitors
    # ------------------------------------------------------------------ #
    def on_stream_state(self, kind: str, old: object, new: object) -> None:
        """Wire to WebSocketManager.add_status_listener."""
        state = getattr(new, "value", str(new))
        if state == "AUTH_FAILED":
            self.kill_switch.trigger(KillReason.AUTH_FAILURE, f"{kind} websocket authentication failed")
        elif state == "FAILED":
            self.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT, f"{kind} websocket reconnects exhausted")
        elif state == "CONNECTED":
            if self._ws_timer is not None:
                self._ws_timer.cancel()
                self._ws_timer = None
        elif getattr(old, "value", str(old)) == "CONNECTED" and kind == "market_data":
            self._start_ws_grace_timer(kind)

    def _start_ws_grace_timer(self, kind: str) -> None:
        if self._ws_timer is not None:
            return
        grace = self._ks_cfg.websocket_disconnect_grace_seconds

        async def _expire() -> None:
            await asyncio.sleep(grace)
            self._ws_timer = None
            self.kill_switch.trigger(KillReason.WEBSOCKET_DISCONNECT,
                                     f"{kind} websocket down for more than {grace:.0f}s")

        with contextlib.suppress(RuntimeError):
            self._ws_timer = asyncio.get_running_loop().create_task(_expire())

    def on_auth_failure(self, reason: str) -> None:
        self.kill_switch.trigger(KillReason.AUTH_FAILURE, reason)

    def on_rate_limit_trip(self, category: object) -> None:
        self.kill_switch.trigger(KillReason.RATE_LIMIT_CIRCUIT, f"{getattr(category, 'value', category)} circuit "
                                 "locked after repeated HTTP 429")

    def record_api_result(self, ok: bool, what: str = "broker API") -> None:
        self._api_failures = 0 if ok else self._api_failures + 1
        if self._api_failures >= self._ks_cfg.max_consecutive_api_failures:
            self.kill_switch.trigger(KillReason.API_FAILURE, f"{self._api_failures} consecutive {what} failures")

    def check_spread(self, tick: Tick, held: bool) -> None:
        """Abnormal spread on a HELD contract for N consecutive ticks -> kill."""
        key = tick.instrument_key
        if not held:
            self._spread_strikes.pop(key, None)
            return
        mid = tick.mid
        pct = 100 * tick.spread / mid if (tick.spread is not None and mid) else None
        if pct is not None and pct <= self._ks_cfg.abnormal_spread_pct:
            self._spread_strikes[key] = 0
            return
        if pct is None:
            return  # one-sided quote: handled by stale/quote checks, not counted
        n = self._spread_strikes.get(key, 0) + 1
        self._spread_strikes[key] = n
        if n >= self._ks_cfg.abnormal_spread_consecutive_ticks:
            self.kill_switch.trigger(KillReason.ABNORMAL_SPREAD,
                                     f"{key} spread {pct:.2f}% > {self._ks_cfg.abnormal_spread_pct}% for {n} ticks")
