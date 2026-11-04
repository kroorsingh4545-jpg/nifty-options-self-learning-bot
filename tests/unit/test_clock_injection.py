"""Every order, fill, position, trade, risk-event and strategy timestamp comes
from the injected clock, so backtests on simulated time never persist the
wall clock (Phase 4/5 carried-over warning)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from backtest.backtester import BacktestOptions, run_backtest
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from core.clock import IST
from core.enums import (
    OptionType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskSeverity,
    StrategyStatus,
    TradingMode,
)
from core.event_bus import EventBus, Topic
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.records import (
    DailyPnlRecord,
    FillRecord,
    OrderRecord,
    PositionRecord,
    RiskEventRecord,
    StrategyRecord,
    TradeRecord,
)
from data.storage.repositories import Repositories
from tests.fakes import FakeWallClock

SIM = datetime(2024, 3, 5, 10, 30, tzinfo=IST)  # far from the real date


def _order(**kw: object) -> OrderRecord:
    base: dict[str, object] = dict(client_order_id="PAPER-O-orb_options-1", mode=TradingMode.PAPER,
                                   paper_order_id="PO-1", strategy_id="orb_options", instrument_key="NSE_FO|1",
                                   trading_symbol="NIFTY X", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                                   purpose=OrderPurpose.ENTRY, quantity=65, price=100.0)
    base.update(kw)
    return OrderRecord(**base)  # type: ignore[arg-type]


def _trade(**kw: object) -> TradeRecord:
    base: dict[str, object] = dict(trade_uid="PAPER-T-orb_options-1", mode=TradingMode.PAPER,
                                   strategy_id="orb_options", strategy_version="0.1.0", instrument_key="NSE_FO|1",
                                   trading_symbol="NIFTY X", option_type=OptionType.CE, strike=24500.0,
                                   expiry=date(2024, 3, 7), side=OrderSide.BUY, quantity=65, initial_stop_loss=75.0,
                                   stop_loss=75.0, target=150.0)
    base.update(kw)
    return TradeRecord(**base)  # type: ignore[arg-type]


@pytest.fixture
def clocked(seeded_db: Database) -> tuple[Repositories, FakeWallClock]:
    clock = FakeWallClock(SIM)
    return Repositories(seeded_db, TradingMode.PAPER, clock=clock), clock


def test_defaulted_record_timestamps_are_stamped_with_the_repository_clock(
        clocked: tuple[Repositories, FakeWallClock]) -> None:
    repos, _ = clocked
    oid = repos.orders.insert(_order())
    repos.fills.insert(FillRecord(order_id=oid, mode=TradingMode.PAPER, strategy_id="orb_options",
                                  paper_fill_id="PF-1", quantity=65, price=100.0))
    repos.trades.insert(_trade())
    pid = repos.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options",
                                                instrument_key="NSE_FO|1", trading_symbol="NIFTY X", quantity=65,
                                                average_price=100.0, stop_loss=75.0))
    repos.risk_events.insert(RiskEventRecord(mode=TradingMode.PAPER, event_type="X", severity=RiskSeverity.INFO,
                                             message="m"))
    repos.daily_pnl.upsert(DailyPnlRecord(trade_date=SIM.date(), mode=TradingMode.PAPER, strategy_id="orb_options"))
    o = repos.orders.get("PAPER-O-orb_options-1")
    assert o is not None and o.created_at == SIM and o.updated_at == SIM
    assert repos.fills.list_all()[0].filled_at == SIM
    t = repos.trades.get("PAPER-T-orb_options-1")
    assert t is not None and t.created_at == SIM and t.updated_at == SIM
    pos = repos.positions.list_open()[0]
    assert pos.id == pid and pos.opened_at == SIM and pos.updated_at == SIM
    assert repos.risk_events.recent()[0].created_at == SIM
    d = repos.daily_pnl.get(SIM.date(), "orb_options")
    assert d is not None and d.updated_at == SIM


def test_explicit_timestamps_are_kept(clocked: tuple[Repositories, FakeWallClock]) -> None:
    repos, _ = clocked
    explicit = SIM - timedelta(hours=1)
    repos.orders.insert(_order(created_at=explicit))
    o = repos.orders.get("PAPER-O-orb_options-1")
    assert o is not None and o.created_at == explicit and o.updated_at == SIM


def test_updates_and_closes_use_the_repository_clock(clocked: tuple[Repositories, FakeWallClock]) -> None:
    repos, clock = clocked
    repos.orders.insert(_order())
    repos.trades.insert(_trade())
    pid = repos.positions.insert(PositionRecord(mode=TradingMode.PAPER, strategy_id="orb_options",
                                                instrument_key="NSE_FO|1", trading_symbol="NIFTY X", quantity=65,
                                                average_price=100.0, stop_loss=75.0))
    clock.advance(minutes=7)
    later = clock.now
    repos.orders.update_status("PAPER-O-orb_options-1", OrderStatus.COMPLETE, filled_quantity=65, average_price=100.0)
    repos.trades.update("PAPER-T-orb_options-1", entry_price=100.0, entry_time=SIM)
    repos.positions.close(pid, realized_pnl=10.0)
    repos.trades.close("PAPER-T-orb_options-1", exit_price=110.0, exit_reason="TARGET", gross_pnl=650.0, charges=40.0)
    repos.strategies.set_status("orb_options", StrategyStatus.PAUSED, at=later)
    o = repos.orders.get("PAPER-O-orb_options-1")
    t = repos.trades.get("PAPER-T-orb_options-1")
    assert o is not None and o.updated_at == later
    assert t is not None and t.exit_time == later and t.updated_at == later
    row = repos.db.query_one("SELECT closed_at, updated_at FROM positions WHERE id = ?", (pid,))
    assert row is not None and row["closed_at"] == later.isoformat() == row["updated_at"]
    s = repos.strategies.get("orb_options")
    assert s is not None and s.updated_at == later


def test_sync_strategies_uses_the_repository_clock(db: Database) -> None:
    cfg = load_config(use_default_env_file=False)
    repos = Repositories(db, TradingMode.PAPER, clock=FakeWallClock(SIM))
    sync_strategies(repos.strategies, cfg.strategies.strategies)
    assert {r.created_at for r in repos.strategies.list_all()} == {SIM}
    assert {r.updated_at for r in repos.strategies.list_all()} == {SIM}


def test_with_clock_shares_the_database(clocked: tuple[Repositories, FakeWallClock]) -> None:
    repos, _ = clocked
    other = FakeWallClock(SIM + timedelta(days=1))
    rebound = repos.with_clock(other)
    assert rebound.db is repos.db and rebound.mode is repos.mode and rebound.clock is other
    assert repos.with_clock(repos.clock) is repos
    rebound.orders.insert(_order())
    o = repos.orders.get("PAPER-O-orb_options-1")
    assert o is not None and o.created_at == other.now


def test_strategy_upsert_stamps_and_keeps_created_at(clocked: tuple[Repositories, FakeWallClock]) -> None:
    repos, clock = clocked
    repos.strategies.upsert(StrategyRecord(strategy_id="new_strat", name="n", version="0.1.0",
                                           allocated_capital=1000.0))
    clock.advance(hours=2)
    repos.strategies.upsert(StrategyRecord(strategy_id="new_strat", name="n", version="0.1.1",
                                           allocated_capital=1000.0))
    s = repos.strategies.get("new_strat")
    assert s is not None and s.created_at == SIM and s.updated_at == SIM + timedelta(hours=2)


def test_event_bus_uses_the_injected_clock() -> None:
    bus = EventBus(now_fn=FakeWallClock(SIM))
    assert bus.publish(Topic.SIGNAL, {}).at == SIM


TIMESTAMP_COLUMNS = {
    "orders": ("created_at", "updated_at"), "fills": ("filled_at",),
    "positions": ("opened_at", "closed_at", "updated_at"),
    "trades": ("entry_time", "exit_time", "created_at", "updated_at"), "risk_events": ("created_at",),
    "daily_pnl": ("updated_at",), "strategies": ("created_at", "updated_at"),
}


def test_backtest_persists_only_simulated_time(tmp_path: Path) -> None:
    """A full backtest on 2026-09 dates: every timestamp in every execution
    table falls on a simulated trading day, inside market hours."""
    cfg = load_config(use_default_env_file=False)
    spec = SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=2, seed=14)
    ds = generate_synthetic_dataset(spec, cfg.settings.market_session)
    path = tmp_path / "bt.db"
    res = run_backtest(cfg, ds, BacktestOptions(database_path=path))
    assert res.closed_trades(), "fixture must trade"
    days = {d.isoformat() for d in res.days}
    db = Database(path)
    try:
        seen = 0
        for table, cols in TIMESTAMP_COLUMNS.items():
            for row in db.query(f"SELECT {', '.join(cols)} FROM {table}"):  # noqa: S608 - fixed names
                for col in cols:
                    if row[col] is None:
                        continue
                    ts = datetime.fromisoformat(row[col])
                    assert ts.date().isoformat() in days, f"{table}.{col} = {row[col]} is not simulated time"
                    assert datetime.combine(ts.date(), datetime.min.time(), tzinfo=IST).replace(hour=9) <= ts, \
                        f"{table}.{col} = {row[col]}"
                    seen += 1
        assert seen > 20
    finally:
        db.close()
