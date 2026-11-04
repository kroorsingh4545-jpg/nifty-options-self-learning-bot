"""Data-access layer: round trips, mode isolation, strategy isolation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.loader import load_config
from core.clock import IST, now_ist
from core.enums import (
    ModelRunStatus,
    ModelType,
    OptionType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskSeverity,
    SnapshotPurpose,
    StrategyStatus,
    TradeStatus,
    TradingMode,
)
from core.ids import new_client_order_id, new_paper_fill_id, new_paper_order_id, new_trade_uid, new_uid
from data.storage.bootstrap import initialize_database
from data.storage.records import (
    DailyPnlRecord,
    FeatureSnapshotRecord,
    FillRecord,
    LearningEventRecord,
    ModelRunRecord,
    OrderRecord,
    PositionRecord,
    RiskEventRecord,
    TradeRecord,
)
from data.storage.repositories import ModeIsolationError, NotFoundError, Repositories


def paper_order(strategy_id: str = "orb_options", **over: object) -> OrderRecord:
    data: dict[str, object] = {
        "client_order_id": new_client_order_id(TradingMode.PAPER, strategy_id),
        "mode": TradingMode.PAPER,
        "paper_order_id": new_paper_order_id(),
        "strategy_id": strategy_id,
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "NIFTY 24500 CE",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "purpose": OrderPurpose.ENTRY,
        "quantity": 75,
    }
    data.update(over)
    return OrderRecord(**data)  # type: ignore[arg-type]


def paper_trade(strategy_id: str = "orb_options", **over: object) -> TradeRecord:
    data: dict[str, object] = {
        "trade_uid": new_trade_uid(TradingMode.PAPER, strategy_id),
        "mode": TradingMode.PAPER,
        "strategy_id": strategy_id,
        "strategy_version": "0.1.0",
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "NIFTY 24500 CE",
        "option_type": OptionType.CE,
        "strike": 24500,
        "expiry": date(2026, 11, 5),
        "side": OrderSide.BUY,
        "quantity": 75,
        "entry_price": 120.0,
        "entry_time": now_ist(),
        "initial_stop_loss": 90.0,
        "stop_loss": 90.0,
        "target": 180.0,
        "trailing_config_json": {"mode": "R_MULTIPLE", "activation": 1.0, "step": 0.5},
    }
    data.update(over)
    return TradeRecord(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Record-level validation (mirrors DB constraints)
# --------------------------------------------------------------------------- #


def test_paper_order_record_requires_paper_id() -> None:
    with pytest.raises(ValidationError, match="paper_order_id"):
        paper_order(paper_order_id=None)


def test_paper_order_record_rejects_broker_id() -> None:
    with pytest.raises(ValidationError, match="broker_order_id"):
        paper_order(broker_order_id="2401")


def test_live_order_record_rejects_paper_id() -> None:
    with pytest.raises(ValidationError):
        OrderRecord(
            client_order_id=new_client_order_id(TradingMode.LIVE, "orb_options"),
            mode=TradingMode.LIVE, paper_order_id="PO-1", strategy_id="orb_options", instrument_key="K",
            trading_symbol="S", side=OrderSide.BUY, order_type=OrderType.MARKET, purpose=OrderPurpose.ENTRY,
            quantity=75,
        )


def test_order_record_rejects_wrong_prefix() -> None:
    with pytest.raises(ValidationError, match="PAPER-"):
        paper_order(client_order_id="LIVE-orb_options-abc")


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError, match="naive"):
        paper_order(created_at=datetime(2026, 1, 1, 9, 15))


def test_open_position_record_requires_stop_loss() -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options", instrument_key="K", trading_symbol="S",
                       quantity=75, average_price=100)


def test_live_approved_model_record_requires_approver() -> None:
    with pytest.raises(ValidationError, match="approver"):
        ModelRunRecord(run_id="r", model_type=ModelType.SIGNAL_FILTER, model_version="v1",
                       feature_schema_version="1", feature_schema_json={},
                       training_start=datetime(2026, 1, 1, tzinfo=IST), training_end=datetime(2026, 6, 1, tzinfo=IST),
                       artifact_path="m", status=ModelRunStatus.LIVE_APPROVED)


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


def test_order_round_trip_and_status_update(paper: Repositories) -> None:
    order = paper_order()
    oid = paper.orders.insert(order)
    got = paper.orders.get(order.client_order_id)
    assert got is not None and got.id == oid
    assert got.status is OrderStatus.CREATED
    assert got.created_at.tzinfo is not None

    paper.orders.update_status(order.client_order_id, OrderStatus.PARTIALLY_FILLED, filled_quantity=25,
                               average_price=120.5)
    got = paper.orders.get(order.client_order_id)
    assert got is not None
    assert got.status is OrderStatus.PARTIALLY_FILLED
    assert got.filled_quantity == 25
    assert got.average_price == pytest.approx(120.5)


def test_update_status_unknown_order(paper: Repositories) -> None:
    with pytest.raises(NotFoundError):
        paper.orders.update_status("PAPER-orb_options-missing", OrderStatus.OPEN)


def test_paper_order_cannot_get_broker_id(paper: Repositories) -> None:
    order = paper_order()
    paper.orders.insert(order)
    with pytest.raises(ModeIsolationError):
        paper.orders.update_status(order.client_order_id, OrderStatus.OPEN, broker_order_id="2401")


def test_partial_fills_round_trip(paper: Repositories) -> None:
    order = paper_order()
    oid = paper.orders.insert(order)
    for qty in (25, 50):
        paper.fills.insert(FillRecord(order_id=oid, mode=TradingMode.PAPER, strategy_id="orb_options",
                                      paper_fill_id=new_paper_fill_id(), quantity=qty, price=120.0, charges=5.0,
                                      slippage=0.5))
    fills = paper.fills.list_for_order(oid)
    assert [f.quantity for f in fills] == [25, 50]


def test_trade_and_linked_order(paper: Repositories) -> None:
    trade = paper_trade()
    paper.trades.insert(trade)
    paper.orders.insert(paper_order(trade_uid=trade.trade_uid))
    got = paper.trades.get(trade.trade_uid)
    assert got is not None
    assert got.trailing_config_json["mode"] == "R_MULTIPLE"
    assert got.expiry == date(2026, 11, 5)
    assert paper.trades.count_entries_on("orb_options", now_ist().date()) == 1

    paper.trades.close(trade.trade_uid, exit_price=150.0, exit_reason="TARGET", gross_pnl=2250.0, charges=60.0,
                       r_multiple=1.0)
    closed = paper.trades.get(trade.trade_uid)
    assert closed is not None and closed.status is TradeStatus.CLOSED
    assert closed.net_pnl == pytest.approx(2190.0)


def test_position_lifecycle(paper: Repositories) -> None:
    pid = paper.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options",
                                                instrument_key="K", trading_symbol="S", quantity=75,
                                                average_price=120.0, stop_loss=90.0))
    paper.positions.update(pid, last_price=130.0, unrealized_pnl=750.0, trailing_stop=110.0)
    (pos,) = paper.positions.list_open("orb_options")
    assert pos.trailing_stop == 110.0
    paper.positions.close(pid, realized_pnl=750.0)
    assert paper.positions.list_open() == []
    with pytest.raises(ValueError, match="cannot update"):
        paper.positions.update(pid, strategy_id="vwap_ema")


def test_daily_pnl_upsert_and_global_view(paper: Repositories) -> None:
    day = date(2026, 11, 3)
    paper.daily_pnl.upsert(DailyPnlRecord(trade_date=day, mode=TradingMode.PAPER, strategy_id="orb_options",
                                          realized_pnl=-1500, trades_count=1, losses=1, consecutive_losses=1))
    paper.daily_pnl.upsert(DailyPnlRecord(trade_date=day, mode=TradingMode.PAPER, strategy_id="orb_options",
                                          realized_pnl=-3000, trades_count=2, losses=2, consecutive_losses=2))
    paper.daily_pnl.upsert(DailyPnlRecord(trade_date=day, mode=TradingMode.PAPER, strategy_id="vwap_ema",
                                          realized_pnl=1000, unrealized_pnl=200, trades_count=1, wins=1))
    orb = paper.daily_pnl.get(day, "orb_options")
    assert orb is not None and orb.realized_pnl == -3000 and orb.consecutive_losses == 2
    g = paper.daily_pnl.global_for_day(day)
    assert g is not None
    assert g["total_pnl"] == pytest.approx(-1800)
    assert g["trades_count"] == 3


def test_risk_events_recent(paper: Repositories) -> None:
    for i in range(5):
        paper.risk_events.insert(RiskEventRecord(mode=TradingMode.PAPER, strategy_id="orb_options",
                                                 event_type="COOLDOWN", severity=RiskSeverity.INFO,
                                                 message=f"e{i}", details_json={"i": i}))
    paper.risk_events.insert(RiskEventRecord(mode=TradingMode.PAPER, event_type="GLOBAL_LOSS_BREACH",
                                             severity=RiskSeverity.CRITICAL, message="global"))
    recent = paper.risk_events.recent(limit=3)
    assert [e.message for e in recent] == ["global", "e4", "e3"]
    assert recent[0].strategy_id is None
    assert len(paper.risk_events.recent(strategy_id="orb_options")) == 5


def test_feature_snapshots(paper: Repositories) -> None:
    trade = paper_trade()
    paper.trades.insert(trade)
    t0 = now_ist()
    for purpose, t in ((SnapshotPurpose.ENTRY, t0), (SnapshotPurpose.EXIT, t0 + timedelta(minutes=20))):
        paper.feature_snapshots.insert(FeatureSnapshotRecord(
            mode=TradingMode.PAPER, strategy_id="orb_options", trade_uid=trade.trade_uid,
            instrument_key="K", purpose=purpose, snapshot_time=t, feature_schema_version="1",
            features_json={"ret_1m": 0.001, "vwap_dist": 0.2}))
    snaps = paper.feature_snapshots.list_for_trade(trade.trade_uid)
    assert [s.purpose for s in snaps] == [SnapshotPurpose.ENTRY, SnapshotPurpose.EXIT]
    assert snaps[0].features_json["vwap_dist"] == 0.2


def test_model_runs(paper: Repositories) -> None:
    run = ModelRunRecord(run_id=new_uid("RUN"), model_type=ModelType.SIGNAL_FILTER, model_version="0.1.0",
                         strategy_id="orb_options", feature_schema_version="1",
                         feature_schema_json={"features": ["ret_1m"]},
                         training_start=datetime(2025, 1, 1, tzinfo=IST),
                         training_end=datetime(2025, 12, 31, tzinfo=IST),
                         hyperparameters_json={"num_leaves": 31}, metrics_json={"auc": 0.58},
                         seeds_json=[1, 2, 3, 4, 5], artifact_path="models/sf/0.1.0")
    paper.model_runs.insert(run)
    got = paper.model_runs.get(run.run_id)
    assert got is not None and got.seeds_json == [1, 2, 3, 4, 5]
    with pytest.raises(ValueError, match="approver"):
        paper.model_runs.set_status(run.run_id, ModelRunStatus.LIVE_APPROVED)
    paper.model_runs.set_status(run.run_id, ModelRunStatus.PAPER_READY)
    assert paper.model_runs.list(ModelType.SIGNAL_FILTER)[0].status is ModelRunStatus.PAPER_READY


def test_learning_events(paper: Repositories) -> None:
    from core.enums import LearningStage

    for stage in (LearningStage.LOG, LearningStage.REFLECT, LearningStage.HYPOTHESIZE):
        paper.learning_events.insert(LearningEventRecord(event_uid=new_uid("LE"), strategy_id="vwap_ema",
                                                         stage=stage, payload_json={"stage": stage.value}))
    events = paper.learning_events.list_for_strategy("vwap_ema")
    assert [e.stage.value for e in events] == ["LOG", "REFLECT", "HYPOTHESIZE"]


# --------------------------------------------------------------------------- #
# Mode isolation
# --------------------------------------------------------------------------- #


def test_paper_repo_refuses_live_records(paper: Repositories) -> None:
    live_order = OrderRecord(
        client_order_id=new_client_order_id(TradingMode.LIVE, "orb_options"), mode=TradingMode.LIVE,
        strategy_id="orb_options", instrument_key="K", trading_symbol="S", side=OrderSide.BUY,
        order_type=OrderType.MARKET, purpose=OrderPurpose.ENTRY, quantity=75,
    )
    with pytest.raises(ModeIsolationError):
        paper.orders.insert(live_order)
    with pytest.raises(ModeIsolationError):
        paper.risk_events.insert(RiskEventRecord(mode=TradingMode.LIVE, event_type="X",
                                                 severity=RiskSeverity.INFO, message="m"))


def test_live_repo_cannot_see_paper_rows(paper: Repositories, live: Repositories) -> None:
    order = paper_order()
    paper.orders.insert(order)
    trade = paper_trade()
    paper.trades.insert(trade)
    paper.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options", instrument_key="K",
                                          trading_symbol="S", quantity=75, average_price=1, stop_loss=0.5))
    assert live.orders.get(order.client_order_id) is None
    assert live.orders.list_for_strategy("orb_options") == []
    assert live.trades.get(trade.trade_uid) is None
    assert live.positions.list_open() == []
    with pytest.raises(NotFoundError):
        live.orders.update_status(order.client_order_id, OrderStatus.CANCELLED)
    assert paper.orders.get(order.client_order_id).status is OrderStatus.CREATED  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Strategy isolation
# --------------------------------------------------------------------------- #


def test_strategies_do_not_see_each_others_orders_or_positions(paper: Repositories) -> None:
    paper.orders.insert(paper_order("orb_options"))
    paper.orders.insert(paper_order("vwap_ema"))
    paper.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="vwap_ema", instrument_key="K",
                                          trading_symbol="S", quantity=75, average_price=1, stop_loss=0.5))
    assert len(paper.orders.list_for_strategy("orb_options")) == 1
    assert len(paper.orders.list_for_strategy("vwap_ema")) == 1
    assert paper.positions.list_open("orb_options") == []
    assert len(paper.positions.list_open("vwap_ema")) == 1


def test_filter_orders_by_status(paper: Repositories) -> None:
    a, b = paper_order(), paper_order()
    paper.orders.insert(a)
    paper.orders.insert(b)
    paper.orders.update_status(b.client_order_id, OrderStatus.REJECTED, status_message="margin")
    pending = paper.orders.list_for_strategy("orb_options", [OrderStatus.CREATED, OrderStatus.OPEN])
    assert [o.client_order_id for o in pending] == [a.client_order_id]


# --------------------------------------------------------------------------- #
# Bootstrap from config
# --------------------------------------------------------------------------- #


def test_initialize_database_syncs_strategies(config_dir: Path, edit_yaml: object, tmp_path: Path) -> None:
    def to_tmp_db(d: dict[str, dict[str, str]]) -> None:
        d["database"]["path"] = str(tmp_path / "bot.db")

    edit_yaml("settings.yaml", to_tmp_db)  # type: ignore[operator]
    cfg = load_config(config_dir, use_default_env_file=False)
    repos = initialize_database(cfg)
    try:
        strategies = {s.strategy_id: s for s in repos.strategies.list_all()}
        assert set(strategies) == {"orb_options", "vwap_ema"}
        assert all(s.allocated_capital == 100_000 for s in strategies.values())
        assert all(s.status is StrategyStatus.ACTIVE for s in strategies.values())
        assert repos.mode is TradingMode.PAPER

        # Intraday PAUSED status survives a restart; config disable wins.
        repos.strategies.set_status("orb_options", StrategyStatus.PAUSED, at=now_ist())
        repos.db.close()
        repos = initialize_database(cfg)
        assert repos.strategies.get("orb_options").status is StrategyStatus.PAUSED  # type: ignore[union-attr]
    finally:
        repos.db.close()


def test_position_status_enum_round_trip(paper: Repositories) -> None:
    pid = paper.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options",
                                                instrument_key="K", trading_symbol="S", quantity=0,
                                                average_price=0))
    paper.positions.close(pid, realized_pnl=0)
    row = paper.db.query_one("SELECT status FROM positions WHERE id = ?", (pid,))
    assert row is not None and row["status"] == PositionStatus.CLOSED.value
