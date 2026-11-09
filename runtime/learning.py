"""Wires the LOG stage of the Reflective Loop into a TradingBot (Phase 9).

* ENTRY snapshot: taken when risk has approved a signal and BEFORE the entry
  order exists (``capture_entry``, called by the gateway): the market features
  at that moment (``ml.feature_engineering``, the same code as training) and
  the selected contract's quote stamped at or before it. Nothing about the
  fill or what the market did afterwards can be in it, so it is safe to learn
  from.
* EXIT snapshot: on the first completed 1-minute NIFTY index candle after a
  trade CLOSES, then its ``trade_journal`` row
  (``learning.trade_journal.TradeJournal``).

``flush`` journals anything still pending (end of a backtest, shutdown), and
start-up backfills closed trades from earlier sessions. Those (and any trade
created before this session started) are labelled UNKNOWN: only trades opened
in this session carry its evidence label.

Strictly read-only towards trading: it reads ``PositionManager.trades`` and
writes only ``feature_snapshots`` and ``trade_journal``. Any failure is
logged and never reaches orders, risk or the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.enums import EvidenceSource, SnapshotPurpose, TradeStatus
from core.models import Candle, Instrument, Signal
from execution.position_manager import ManagedTrade
from learning.trade_journal import TradeJournal, config_risk_fn, record_trade_snapshot
from ml.feature_engineering import FeatureEngine
from monitoring.json_logger import get_logger
from runtime.ml import contract_context, feature_engine

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)


@dataclass
class LearningRecorder:
    bot: TradingBot
    features: FeatureEngine
    journal: TradeJournal
    journaled: set[str] = field(default_factory=set)
    failures: int = 0

    def on_candle(self, candle: Candle) -> None:
        if candle.timeframe_minutes != 1 or not candle.complete or candle.instrument_key != self.bot.spot_key:
            return
        self.scan()

    def capture_entry(self, signal: Signal, instrument: Instrument) -> tuple[datetime, Mapping[str, Any]]:
        """Market features now, before the entry order exists. Never raises."""
        as_of = self.bot._now()
        try:
            option = contract_context(self.bot, instrument, as_of)
            return as_of, dict(self.features.features(as_of, direction=signal.option_type, option=option))
        except Exception as exc:  # e.g. not enough bars yet: still record that we tried
            return as_of, {"error": f"{type(exc).__name__}: {exc}"[:300]}

    def bind_entry(self, trade: ManagedTrade, captured: tuple[datetime, Mapping[str, Any]]) -> None:
        """Persist the captured ENTRY snapshot once the trade exists. Never raises."""
        as_of, feats = captured
        try:
            record_trade_snapshot(self.bot.repos, trade, SnapshotPurpose.ENTRY, as_of, feats,
                                  instrument_key=trade.instrument.instrument_key)
        except Exception:
            self.failures += 1
            log.exception("entry feature snapshot failed (trading is unaffected)",
                          extra={"strategy_id": trade.strategy_id, "trade": trade.trade_uid})

    def _features(self, t: ManagedTrade, as_of: datetime) -> dict[str, Any]:
        option = contract_context(self.bot, t.instrument, as_of)
        feats: dict[str, Any] = dict(self.features.features(as_of, direction=t.option_type, option=option))
        feats.update({"trade_entry_price": t.avg_entry, "trade_stop": t.stop, "trade_target": t.levels.target,
                      "trade_last_price": t.last_price, "trade_open_qty": t.open_qty})
        return feats

    def _snapshot(self, t: ManagedTrade, purpose: SnapshotPurpose, as_of: datetime) -> None:
        try:
            feats = self._features(t, as_of)
        except Exception as exc:  # e.g. not enough bars yet: still record that we tried
            feats = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        record_trade_snapshot(self.bot.repos, t, purpose, as_of, feats, instrument_key=t.instrument.instrument_key)

    def scan(self) -> None:
        now = self.bot._now()
        for t in list(self.bot.stack.positions.trades.values()):
            uid = t.trade_uid
            try:
                if t.status is TradeStatus.CLOSED and uid not in self.journaled:
                    self.journaled.add(uid)
                    self._snapshot(t, SnapshotPurpose.EXIT, now)
                    self.journal.record(uid)
            except Exception:
                self.failures += 1
                log.exception("trade journal write failed (trading is unaffected)",
                              extra={"strategy_id": t.strategy_id, "trade": uid})

    def flush(self) -> int:
        """Journal every closed trade not journaled yet (end of run / shutdown)."""
        self.scan()
        try:
            return self.journal.backfill(self.bot.cfg.strategies.strategies)
        except Exception:
            self.failures += 1
            log.exception("trade journal backfill failed (trading is unaffected)")
            return 0

    def stats(self) -> dict[str, Any]:
        return {"journaled": len(self.journaled), "failures": self.failures,
                "data_source": self.journal.data_source.value}


def build_learning(bot: TradingBot, *, data_source: EvidenceSource,
                   features: FeatureEngine | None = None) -> LearningRecorder:
    # Trades created before this session (earlier sessions, other feeds, pre-Phase 9)
    # are journaled as UNKNOWN evidence, never with this session's label.
    journal = TradeJournal(bot.repos, data_source=data_source, risk_config_fn=config_risk_fn(bot.cfg),
                           session_start=bot._now())
    rec = LearningRecorder(bot, features or feature_engine(bot), journal)
    try:
        n = journal.backfill(bot.cfg.strategies.strategies)
        if n:
            log.info("journaled closed trades from earlier sessions", extra={"trades": n})
    except Exception:
        log.exception("trade journal backfill failed at start-up (trading is unaffected)")
    bot.candles.add_listener(rec.on_candle)  # after the feature engine's own listener
    return rec


__all__ = ["LearningRecorder", "build_learning"]
