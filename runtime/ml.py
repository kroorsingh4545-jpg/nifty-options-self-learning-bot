"""Wires the ML inference service into a TradingBot (composition root).

* The bot's ``CandleBuilder`` feeds completed 1-minute NIFTY index and
  current-future candles into a ``FeatureEngine``.
* The option context for a signal is the contract the strategy's strike rules
  select from the subscribed universe (the same rules
  ``OptionContractResolver`` applies right after the filter), with its live
  quote. A quote stamped after the decision time is never used.
* Recent outcomes are the strategy's own CLOSED trades.
* Feature snapshots go to the bot's ``feature_snapshots`` table.

Nothing here can place an order: the inference service only returns a
``FilterDecision`` that the engine passes on to the risk gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from core.enums import TradeStatus
from core.models import Signal
from ml.dataset import session_times
from ml.feature_engineering import FeatureEngine, OptionContext, PastOutcome
from ml.feature_store import FeatureStore
from ml.inference_service import InferenceService, StrategyModels

if TYPE_CHECKING:
    from runtime.bot import TradingBot


def build_inference(bot: TradingBot, models: Mapping[str, StrategyModels], *, research: bool = False) -> \
        InferenceService:
    cfg = bot.cfg
    engine = FeatureEngine(index_key=bot.spot_key, times=session_times(cfg),
                           futures_key=bot.futures.instrument_key if bot.futures else None)
    bot.candles.add_listener(engine.on_candle)

    def option_context(signal: Signal, as_of: datetime) -> OptionContext | None:
        universe, spot = bot.universe, bot.spot()
        if universe is None or spot is None:
            return None
        ss = cfg.strategies.strategies[signal.strategy_id].strike_selection
        inst = universe.select(spot, signal.option_type, ss.mode, ss.offset_steps)
        if inst is None or inst.strike is None or inst.expiry is None:
            return None
        tick = bot.md.get(inst.instrument_key)
        if tick is None or tick.exchange_time > as_of:
            return OptionContext(signal.option_type, inst.strike, inst.expiry, as_of=as_of)
        return OptionContext(signal.option_type, inst.strike, inst.expiry, as_of=tick.exchange_time,
                             premium=tick.ltp, day_volume=tick.volume_today, oi=tick.oi, bid=tick.bid, ask=tick.ask,
                             iv=tick.iv)

    def outcomes(strategy_id: str) -> Sequence[PastOutcome]:
        return [PastOutcome(t.exit_time, float(t.net_pnl), t.r_multiple)
                for t in bot.repos.trades.list_for_strategy(strategy_id, TradeStatus.CLOSED)
                if t.exit_time is not None and t.net_pnl is not None]

    return InferenceService(cfg, features=engine, option_context=option_context, outcomes=outcomes, models=models,
                            mode=cfg.mode, research=research, store=FeatureStore(bot.repos.feature_snapshots),
                            now_fn=bot._now)


__all__ = ["build_inference"]
