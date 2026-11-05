"""Wires the LOG stage of the Reflective Loop into a TradingBot (Phase 9).

On every completed 1-minute NIFTY index candle (after the feature engine has
seen it) the recorder looks at the position manager's trades:

* a trade whose entry has filled and that has no ENTRY snapshot yet gets one:
  the market features AS OF that minute (``ml.feature_engineering``, the
  same code as training), the selected contract's quote stamped at or before
  the as-of time, entry price / stop / target;
* a trade that has CLOSED gets an EXIT snapshot the same way, then its
  ``trade_journal`` row (``learning.trade_journal.TradeJournal``).

Snapshots are taken at the first candle after the event, so they lag the
fill by up to one minute; they never use data from after their as-of time.
``flush`` journals anything still pending (end of a backtest, shutdown), and
start-up backfills closed trades from earlier sessions. Those (and any trade
created before this session started) are labelled UNKNOWN: only trades opened
in this session carry its evidence label.

Strictly read-only towards trading: it reads ``PositionManager.trades`` and
writes only ``feature_snapshots`` and ``trade_journal``. Any failure is
logged and never reaches orders, risk or the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.enums import EvidenceSource, SnapshotPurpose, TradeStatus
from core.models import Candle
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
    entry_snapped: set[str] = field(default_factory=set)
    journaled: set[str] = field(default_factory=set)
    failures: int = 0

    def on_candle(self, candle: Candle) -> None:
        if candle.timeframe_minutes != 1 or not candle.complete or candle.instrument_key != self.bot.spot_key:
            return
        self.scan()

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
                if t.entry_complete and t.filled_qty > 0 and uid not in self.entry_snapped:
                    self.entry_snapped.add(uid)
                    self._snapshot(t, SnapshotPurpose.ENTRY, now)
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
    for t in bot.stack.positions.trades.values():  # restored open trades: their entry snapshot is gone
        if t.entry_complete:
            rec.entry_snapped.add(t.trade_uid)
    bot.candles.add_listener(rec.on_candle)  # after the feature engine's own listener
    return rec


__all__ = ["LearningRecorder", "build_learning"]
