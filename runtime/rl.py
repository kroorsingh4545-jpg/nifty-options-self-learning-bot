"""Wires the RL trade manager into a TradingBot (composition root, Phase 8).

* Entry approval: ``RLTradeAdvisor.evaluate`` is chained after the ML signal
  filter (``core.interfaces.ChainedFilter``), so it can only veto. The risk
  gate still sizes and checks every entry.
* Trade management: on every completed 1-minute NIFTY index candle, each OPEN
  trade of an RL-enabled strategy whose decision interval has elapsed is
  turned into a ``TradeContext``; the advisor's ``TradeAdvice`` goes to
  ``PositionManager.apply_advice``, which re-validates it.
* Never consulted while the kill switch blocks the strategy (the kill-switch
  handlers own those exits), at/after the square-off start (the square-off
  manager owns those), or for a trade that is entering or exiting.
* Phase 9: every decision (HOLD included) is persisted to ``rl_decisions``
  with what execution did with it. If a decision cannot be written, the
  advisor suspends RL for that strategy for the session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.enums import RLDecisionKind
from core.interfaces import TradeAdvice, TradeAdviceAction
from core.models import Candle
from execution.position_manager import ManagedTrade
from ml.feature_engineering import FeatureEngine
from ml.inference_service import RLTradeAdvisor, TradeContext
from ml.model_registry import LoadedModel
from ml.rl_environment import AccountView, PositionView, RiskEnvelope
from monitoring.json_logger import get_logger
from runtime.ml import closed_outcomes, contract_context, feature_engine, signal_option_context

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)


def account_view(bot: TradingBot, strategy_id: str) -> AccountView:
    """The strategy's day as the risk layer sees it (read-only)."""
    e = RiskEnvelope.from_config(bot.cfg, strategy_id)
    s = bot.stack.guard.state.get(strategy_id)
    now = bot._now()
    realized = s.realized if s else 0.0
    last_loss = s.last_loss_at if s else None
    cooldown = last_loss is not None and now < last_loss + timedelta(minutes=e.loss_cooldown_minutes)
    return AccountView(open_positions=bot.stack.positions.open_trade_count(strategy_id), realized_pnl_today=realized,
                       max_daily_loss=e.max_daily_loss, trades_today=s.trades if s else 0,
                       max_trades_per_day=e.max_trades_per_day, cooldown_active=cooldown,
                       consecutive_losses=s.consecutive_losses if s else 0, risk_budget=e.risk_per_trade)


def position_view(t: ManagedTrade, entry_time: datetime) -> PositionView:
    lot = t.instrument.lot_size
    return PositionView(option_type=t.option_type, entry=t.avg_entry, initial_stop=t.levels.stop, stop=t.stop,
                        target=t.levels.target, lot_units=lot, lots_initial=max(1, t.filled_qty // lot),
                        lots_open=t.open_qty // lot, entry_time=entry_time, peak=max(t.trail.peak, t.avg_entry),
                        partials_taken=t.partial_exits)


@dataclass
class RLRuntime:
    bot: TradingBot
    advisor: RLTradeAdvisor
    applied: list[dict[str, Any]] = field(default_factory=list)  # recent applied / refused advice (audit)

    def on_candle(self, candle: Candle) -> None:
        if candle.timeframe_minutes != 1 or not candle.complete or candle.instrument_key != self.bot.spot_key:
            return
        self.step()

    def step(self) -> None:
        bot = self.bot
        now = bot._now()
        if now.time() >= bot.cfg.risk.time_rules.square_off_start:
            return
        ks = bot.stack.kill_switch
        for t in bot.stack.positions.open_trades():
            sid = t.strategy_id
            if not self.advisor.enabled(sid) or ks.is_blocked(sid):
                continue
            if not t.entry_complete or t.exit_in_progress or t.open_qty <= 0 or t.entry_time is None:
                continue
            if not self.advisor.due(t.trade_uid, sid, now):
                continue
            ctx = TradeContext(strategy_id=sid, trade_uid=t.trade_uid, position=position_view(t, t.entry_time),
                               option=contract_context(bot, t.instrument, now), premium=t.last_price,
                               tick_size=t.instrument.tick_size, account=account_view(bot, sid))
            advice = self.advisor.advise(ctx)
            if advice.action is TradeAdviceAction.HOLD:
                self._record(t.trade_uid, advice, now, applied=None, result="HOLD")
                continue
            res = bot.stack.positions.apply_advice(advice)
            self._record(t.trade_uid, advice, now, applied=res.applied, result=res.reason)
            self.applied.append({"at": now.isoformat(), "trade": t.trade_uid, "strategy_id": sid,
                                 "action": advice.action.value, "new_stop": advice.new_stop,
                                 "exit_lots": advice.exit_lots, "applied": res.applied, "result": res.reason,
                                 "model": advice.model_version})
            del self.applied[:-200]

    def _record(self, trade_uid: str, advice: TradeAdvice, now: datetime, *, applied: bool | None, result: str) -> None:
        """Persist one management decision. Building and writing the record are
        guarded together (``RLTradeAdvisor.record_decision``): a failure
        suspends RL for the strategy and never escapes the candle listener."""
        info = self.advisor.last.get(advice.strategy_id, {})
        details = {k: v for k, v in info.items() if k not in ("trade", "action", "model", "decided_at", "reason")}
        self.advisor.record_decision(
            kind=RLDecisionKind.MANAGE, strategy_id=advice.strategy_id, trade_uid=trade_uid,
            action=advice.action.value, decided_at=now, reason=advice.reason or "", applied=applied,
            result=result, new_stop=advice.new_stop, exit_lots=advice.exit_lots, confidence=advice.confidence,
            model_version=advice.model_version, details=details)


def build_rl(bot: TradingBot, models: Mapping[str, LoadedModel], *, research: bool = False,
             features: FeatureEngine | None = None) -> RLRuntime:
    cfg = bot.cfg
    advisor = RLTradeAdvisor(cfg, features=features or feature_engine(bot), models=models,
                             option_context=lambda s, t: signal_option_context(bot, s, t),
                             account=lambda sid: account_view(bot, sid),
                             outcomes=lambda sid: closed_outcomes(bot, sid), mode=cfg.mode, research=research,
                             now_fn=bot._now, decisions=bot.repos.rl_decisions)
    rt = RLRuntime(bot, advisor)
    bot.candles.add_listener(rt.on_candle)  # after the feature engine's listener: features include this bar
    return rt


__all__ = ["RLRuntime", "account_view", "build_rl", "position_view"]
