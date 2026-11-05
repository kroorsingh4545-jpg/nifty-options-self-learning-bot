"""LOG stage of the Reflective Loop: the trade journal.

One ``trade_journal`` row per CLOSED trade, assembled from what the bot
already persisted:

* ``trades``: strategy version, entry / exit times and prices, quantity,
  entry and exit reasons, ML confidence, regime, contract, stop / target,
  gross / net P&L, costs, R-multiple
* ``orders`` + ``fills``: slippage (rupees over every fill), execution
  quality (fill counts, partial fills, entry latency, rejected / cancelled /
  unknown orders, exit attempts)
* ``feature_snapshots`` (ENTRY / EXIT, linked to the trade): point-in-time
  features at entry and at exit (``record_trade_snapshot``)
* ``risk_events`` that mention the trade, and failed orders: errors
* ``rl_decisions`` for the trade: RL activity
* the strategy's stop / target / trailing / sizing / strike / ML settings
  for the version that traded (``risk_config``)

Every row carries ``data_source``: LIVE_FEED (PAPER on the real Upstox feed),
HISTORICAL (a backtest on recorded real data), SYNTHETIC or UNKNOWN. Only
LIVE_FEED / HISTORICAL rows are evidence; SYNTHETIC / UNKNOWN rows are
reported (clearly marked) but can never justify a promotion.

The journal only reads trading state; it never touches orders, positions,
risk limits or the kill switch. Rows are append-only (schema triggers).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config.settings import BotConfig, StrategyConfig
from core.clock import ensure_ist
from core.enums import EvidenceSource, OrderPurpose, OrderStatus, SnapshotPurpose, TradeStatus
from data.storage.records import (
    FeatureSnapshotRecord,
    FillRecord,
    OrderRecord,
    RiskEventRecord,
    RLDecisionRecord,
    TradeJournalRecord,
    TradeRecord,
)
from data.storage.repositories import Repositories

log = logging.getLogger(__name__)

#: Snapshot schema of the journal's own ENTRY / EXIT snapshots (market features
#: from ml.feature_engineering plus a few trade fields).
JOURNAL_SNAPSHOT_SCHEMA = "journal-1"

RiskConfigFn = Callable[[TradeRecord], Mapping[str, Any]]


def risk_config(scfg: StrategyConfig) -> dict[str, Any]:
    """The settings a reflection groups trades by (one strategy version)."""
    return {
        "version": scfg.version,
        "stop_loss": scfg.stop_loss.model_dump(mode="json"),
        "take_profit": scfg.take_profit.model_dump(mode="json"),
        "trailing": scfg.trailing.model_dump(mode="json"),
        "size_multiplier": scfg.sizing.size_multiplier,
        "strike_selection": {"mode": scfg.strike_selection.mode, "offset_steps": scfg.strike_selection.offset_steps},
        "entry_window": {"start": scfg.entry_window.start.strftime("%H:%M"),
                         "end": scfg.entry_window.end.strftime("%H:%M")},
        "ml": {"enabled": scfg.ml.enabled, "confidence_threshold": scfg.ml.confidence_threshold},
        "rl": {"enabled": scfg.rl.enabled},
    }


def config_risk_fn(cfg: BotConfig) -> RiskConfigFn:
    """Risk config of the CURRENT strategy config, for trades of that version.
    A trade of another version gets ``{"version": ..., "known": False}``."""

    def fn(t: TradeRecord) -> Mapping[str, Any]:
        scfg = cfg.strategies.strategies.get(t.strategy_id)
        if scfg is None or scfg.version != t.strategy_version:
            return {"version": t.strategy_version, "known": False, "trailing": t.trailing_config_json}
        return risk_config(scfg)

    return fn


def _finite(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def clean_features(features: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): _finite(v) for k, v in features.items()}


def record_trade_snapshot(repos: Repositories, trade: TradeRecord | Any, purpose: SnapshotPurpose, as_of: datetime,
                          features: Mapping[str, Any], *, instrument_key: str) -> int:
    """Persist an ENTRY or EXIT feature snapshot linked to the trade."""
    if purpose is SnapshotPurpose.SIGNAL:
        raise ValueError("trade snapshots are ENTRY or EXIT")
    rec = FeatureSnapshotRecord(mode=repos.mode, strategy_id=trade.strategy_id, trade_uid=trade.trade_uid,
                                instrument_key=instrument_key, purpose=purpose, snapshot_time=ensure_ist(as_of),
                                feature_schema_version=JOURNAL_SNAPSHOT_SCHEMA, features_json=clean_features(features))
    return repos.feature_snapshots.insert(rec)


# --------------------------------------------------------------------------- #
# Building one entry (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeFacts:
    """Everything persisted about one trade (read from the database)."""

    trade: TradeRecord
    orders: Sequence[OrderRecord] = ()
    fills: Sequence[FillRecord] = ()
    snapshots: Sequence[FeatureSnapshotRecord] = ()
    risk_events: Sequence[RiskEventRecord] = ()
    rl_decisions: Sequence[RLDecisionRecord] = ()


def execution_quality(t: TradeRecord, orders: Sequence[OrderRecord], fills: Sequence[FillRecord]) -> dict[str, Any]:
    by_order: dict[int, list[FillRecord]] = {}
    for f in fills:
        by_order.setdefault(f.order_id, []).append(f)
    entry = [o for o in orders if o.purpose is OrderPurpose.ENTRY]
    exits = [o for o in orders if o.purpose is not OrderPurpose.ENTRY]
    entry_fills = [f for o in entry for f in by_order.get(o.id or -1, [])]
    exit_fills = [f for o in exits for f in by_order.get(o.id or -1, [])]
    latency = None
    if entry and entry_fills:
        first = min(f.filled_at for f in entry_fills)
        latency = round((first - entry[0].created_at).total_seconds(), 3)
    slippage_rs = sum(abs(f.slippage) * f.quantity for f in fills)
    risk_rs = max(0.0, (t.entry_price or 0.0) - t.initial_stop_loss) * t.quantity
    statuses = Counter(o.status.value for o in orders)
    return {
        "orders": len(orders),
        "fills": len(fills),
        "entry_fills": len(entry_fills),
        "exit_fills": len(exit_fills),
        "partial_entry": len(entry_fills) > 1,
        "partial_exit": len(exit_fills) > 1,
        "exit_attempts": len([o for o in exits if o.purpose is not OrderPurpose.STOP_LOSS]),
        "exit_purposes": sorted({o.purpose.value for o in exits if o.filled_quantity > 0}),
        "rejected_orders": statuses.get(OrderStatus.REJECTED.value, 0),
        "cancelled_orders": statuses.get(OrderStatus.CANCELLED.value, 0),
        "unknown_orders": statuses.get(OrderStatus.UNKNOWN.value, 0),
        "entry_latency_seconds": latency,
        "slippage_rs": round(slippage_rs, 2),
        "slippage_per_unit": round(slippage_rs / max(1, sum(f.quantity for f in fills)), 4),
        "slippage_pct_of_risk": round(slippage_rs / risk_rs * 100, 2) if risk_rs > 0 else None,
        "costs_pct_of_gross": (round(t.charges / abs(t.gross_pnl) * 100, 2)
                               if t.gross_pnl not in (None, 0) else None),
    }


def trade_errors(orders: Sequence[OrderRecord], events: Sequence[RiskEventRecord]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in orders:
        if o.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
            out.append({"kind": "ORDER", "order": o.client_order_id, "purpose": o.purpose.value,
                        "status": o.status.value, "message": o.status_message or ""})
    for e in events:
        out.append({"kind": "RISK_EVENT", "event_type": e.event_type, "severity": e.severity.value,
                    "message": e.message[:300], "at": e.created_at.isoformat()})
    return out


def rl_activity(decisions: Sequence[RLDecisionRecord]) -> dict[str, Any]:
    if not decisions:
        return {}
    acts = Counter(d.action for d in decisions)
    return {"decisions": len(decisions), "actions": dict(acts),
            "applied": sum(1 for d in decisions if d.applied), "refused": sum(1 for d in decisions
                                                                             if d.applied is False),
            "models": sorted({d.model_version for d in decisions if d.model_version}),
            "research": any(d.research for d in decisions)}


def build_entry(facts: TradeFacts, *, data_source: EvidenceSource, risk_cfg: Mapping[str, Any] | None = None
                ) -> TradeJournalRecord:
    t = facts.trade
    if t.status is not TradeStatus.CLOSED:
        raise ValueError(f"trade {t.trade_uid} is not CLOSED")
    if t.entry_time is None or t.entry_price is None or t.exit_time is None or t.exit_price is None \
            or t.net_pnl is None:
        raise ValueError(f"trade {t.trade_uid} is missing entry / exit details")
    snaps = {s.purpose: s for s in facts.snapshots}  # the last of each purpose wins
    entry_snap = snaps.get(SnapshotPurpose.ENTRY)
    exit_snap = snaps.get(SnapshotPurpose.EXIT)
    ex = execution_quality(t, facts.orders, facts.fills)
    return TradeJournalRecord(
        trade_uid=t.trade_uid, mode=t.mode, strategy_id=t.strategy_id, strategy_version=t.strategy_version,
        data_source=EvidenceSource(data_source), instrument_key=t.instrument_key, trading_symbol=t.trading_symbol,
        option_type=t.option_type, strike=t.strike, expiry=t.expiry, quantity=t.quantity, entry_time=t.entry_time,
        exit_time=t.exit_time, entry_price=t.entry_price, exit_price=t.exit_price, entry_reason=t.entry_reason,
        exit_reason=t.exit_reason, ml_confidence=t.ml_confidence, regime=t.regime,
        initial_stop_loss=t.initial_stop_loss, final_stop_loss=t.stop_loss, target=t.target,
        risk_config_json=dict(risk_cfg or {"version": t.strategy_version, "trailing": t.trailing_config_json}),
        gross_pnl=float(t.gross_pnl or 0.0), costs=float(t.charges), net_pnl=float(t.net_pnl),
        slippage=float(ex["slippage_rs"]), r_multiple=t.r_multiple,
        features_entry_json=({"as_of": entry_snap.snapshot_time.isoformat(), **entry_snap.features_json}
                             if entry_snap else {}),
        features_exit_json=({"as_of": exit_snap.snapshot_time.isoformat(), **exit_snap.features_json}
                            if exit_snap else {}),
        errors_json=trade_errors(facts.orders, facts.risk_events), execution_json=ex,
        rl_json=rl_activity(facts.rl_decisions), created_at=t.exit_time)


# --------------------------------------------------------------------------- #
# The journal (database-backed)
# --------------------------------------------------------------------------- #


class TradeJournal:
    def __init__(self, repos: Repositories, *, data_source: EvidenceSource,
                 risk_config_fn: RiskConfigFn | None = None) -> None:
        self.repos = repos
        self.data_source = EvidenceSource(data_source)
        self._risk_cfg = risk_config_fn

    def facts(self, trade: TradeRecord) -> TradeFacts:
        r = self.repos
        orders = r.orders.list_for_trade(trade.trade_uid)
        fills = [f for o in orders if o.id is not None for f in r.fills.list_for_order(o.id)]
        snaps = [s for s in r.feature_snapshots.list_for_trade(trade.trade_uid)
                 if s.purpose in (SnapshotPurpose.ENTRY, SnapshotPurpose.EXIT)]
        events = r.risk_events.mentioning(trade.trade_uid, trade.strategy_id)
        return TradeFacts(trade, orders, fills, snaps, events, r.rl_decisions.list_for_trade(trade.trade_uid))

    def record(self, trade_uid: str) -> TradeJournalRecord | None:
        """Journal one CLOSED trade (idempotent). None if not closed / unknown."""
        existing = self.repos.journal.get(trade_uid)
        if existing is not None:
            return existing
        t = self.repos.trades.get(trade_uid)
        if t is None or t.status is not TradeStatus.CLOSED:
            return None
        cfg = self._risk_cfg(t) if self._risk_cfg else None
        entry = build_entry(self.facts(t), data_source=self.data_source, risk_cfg=cfg)
        self.repos.journal.insert(entry)
        return self.repos.journal.get(trade_uid)

    def backfill(self, strategy_ids: Iterable[str] | None = None) -> int:
        """Journal every CLOSED trade that is not journaled yet. Returns how many."""
        done = self.repos.journal.journaled_uids()
        sids = list(strategy_ids) if strategy_ids is not None else [s.strategy_id
                                                                     for s in self.repos.strategies.list_all()]
        n = 0
        for sid in sids:
            for t in self.repos.trades.list_for_strategy(sid, TradeStatus.CLOSED):
                if t.trade_uid in done:
                    continue
                try:
                    if self.record(t.trade_uid) is not None:
                        n += 1
                except Exception:
                    log.exception("could not journal a closed trade", extra={"trade": t.trade_uid})
        return n

    def entries(self, strategy_id: str | None = None, *, since: datetime | None = None,
                until: datetime | None = None) -> list[TradeJournalRecord]:
        return self.repos.journal.list(strategy_id, since=since, until=until)


__all__ = ["JOURNAL_SNAPSHOT_SCHEMA", "TradeFacts", "TradeJournal", "build_entry", "clean_features",
           "config_risk_fn", "execution_quality", "record_trade_snapshot", "risk_config", "rl_activity",
           "trade_errors"]
