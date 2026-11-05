"""LOG stage: the trade journal and its runtime recorder, on SYNTHETIC
backtests through the real PAPER bot."""

from __future__ import annotations

import ast
import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from backtest.backtester import Backtester, BacktestOptions, BacktestResult, evidence_of
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import AppSettings, BotConfig
from core.enums import EvidenceSource, OrderStatus, SnapshotPurpose, TradeStatus, TradingMode
from data.storage.database import Database
from data.storage.repositories import Repositories
from learning.trade_journal import TradeJournal, build_entry, config_risk_fn, execution_quality
from tests.ml_support import config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=3, seed=14)


def _run(cfg: BotConfig, tmp: Path | None = None, **kw: Any) -> BacktestResult:
    ds = generate_synthetic_dataset(SPEC, cfg.settings.market_session)
    opts = BacktestOptions(database_path=tmp, **kw)
    return asyncio.run(Backtester(cfg, ds, opts).run())


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[BacktestResult, Path]:
    path = tmp_path_factory.mktemp("journal") / "bt.db"
    return _run(config(), path), path


def test_every_closed_trade_is_journaled_once_and_marked_synthetic(run: tuple[BacktestResult, Path]) -> None:
    res, _ = run
    closed = res.closed_trades()
    assert closed, "the synthetic run trades"
    assert sorted(j.trade_uid for j in res.journal) == sorted(t.trade_uid for t in closed)
    assert {j.data_source for j in res.journal} == {EvidenceSource.SYNTHETIC}
    assert not any(j.is_real_evidence for j in res.journal)


def test_journal_rows_carry_everything_reflection_needs(run: tuple[BacktestResult, Path]) -> None:
    res, _ = run
    trades = {t.trade_uid: t for t in res.trades}
    fills_by_order: dict[int, list[Any]] = {}
    for f in res.fills:
        fills_by_order.setdefault(f.order_id, []).append(f)
    for j in res.journal:
        t = trades[j.trade_uid]
        assert (j.strategy_version, j.entry_time, j.exit_time, j.entry_reason, j.exit_reason) == \
            (t.strategy_version, t.entry_time, t.exit_time, t.entry_reason, t.exit_reason)
        assert (j.entry_price, j.exit_price, j.quantity, j.net_pnl, j.costs, j.r_multiple) == \
            (t.entry_price, t.exit_price, t.quantity, t.net_pnl, t.charges, t.r_multiple)
        assert (j.initial_stop_loss, j.final_stop_loss, j.target) == (t.initial_stop_loss, t.stop_loss, t.target)
        assert j.risk_config_json["stop_loss"]["value"] > 0 and "trailing" in j.risk_config_json
        orders = [o for o in res.orders if o.trade_uid == j.trade_uid]
        slip = sum(abs(f.slippage) * f.quantity for o in orders for f in fills_by_order.get(o.id or -1, []))
        assert j.slippage == pytest.approx(round(slip, 2))
        assert j.execution_json["orders"] == len(orders) and j.execution_json["entry_fills"] >= 1
        # features at entry and exit: point in time, taken at the first minute after the event
        fe, fx = j.features_entry_json, j.features_exit_json
        assert "rv_15m" in fe and "option_moneyness_pct" in fe and "rv_15m" in fx
        from datetime import datetime
        e_at, x_at = datetime.fromisoformat(fe["as_of"]), datetime.fromisoformat(fx["as_of"])
        assert j.entry_time <= e_at <= j.entry_time + timedelta(minutes=1, seconds=1)
        assert j.exit_time <= x_at <= j.exit_time + timedelta(minutes=1, seconds=1)


def test_snapshots_are_linked_to_their_trades(run: tuple[BacktestResult, Path]) -> None:
    res, path = run
    db = Database.open_existing(path)
    repos = Repositories(db, TradingMode.PAPER)
    for j in res.journal:
        purposes = [s.purpose for s in repos.feature_snapshots.list_for_trade(j.trade_uid)]
        assert purposes.count(SnapshotPurpose.ENTRY) == 1 and purposes.count(SnapshotPurpose.EXIT) == 1
    db.close()


def test_journal_is_idempotent_and_only_journals_closed_trades(run: tuple[BacktestResult, Path]) -> None:
    res, path = run
    db = Database.open_existing(path)
    repos = Repositories(db, TradingMode.PAPER)
    cfg = config()
    journal = TradeJournal(repos, data_source=EvidenceSource.SYNTHETIC, risk_config_fn=config_risk_fn(cfg))
    assert journal.backfill(cfg.strategies.strategies) == 0  # everything already journaled
    uid = res.journal[0].trade_uid
    assert journal.record(uid) == repos.journal.get(uid)
    assert journal.record("PAPER-T-orb_options-nope") is None
    t = repos.trades.get(uid)
    assert t is not None
    with pytest.raises(ValueError, match="CLOSED"):
        build_entry(journal.facts(t.model_copy(update={"status": TradeStatus.OPEN})),
                    data_source=EvidenceSource.SYNTHETIC)
    db.close()


def test_execution_quality_flags_failed_orders(run: tuple[BacktestResult, Path]) -> None:
    res, _ = run
    t = res.closed_trades()[0]
    orders = [o for o in res.orders if o.trade_uid == t.trade_uid]
    bad = orders[0].model_copy(update={"status": OrderStatus.REJECTED, "client_order_id": "PAPER-x-rej", "id": -5})
    q = execution_quality(t, [*orders, bad], [f for f in res.fills if f.order_id in {o.id for o in orders}])
    assert q["rejected_orders"] == 1 and q["orders"] == len(orders) + 1


def test_journal_can_be_turned_off() -> None:
    cfg = config()
    settings = AppSettings.model_validate({**cfg.settings.model_dump(), "learning": {"journal_enabled": False}})
    off = BotConfig(project_root=cfg.project_root, settings=settings, risk=cfg.risk, strategies=cfg.strategies,
                    env=cfg.env)
    res = _run(off)
    assert res.closed_trades() and res.journal == []


def test_evidence_labels() -> None:
    assert evidence_of("SYNTHETIC") is EvidenceSource.SYNTHETIC
    assert evidence_of("HISTORICAL") is EvidenceSource.HISTORICAL
    assert evidence_of("whatever") is EvidenceSource.UNKNOWN


def test_a_failing_journal_never_affects_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: TradeJournal, trade_uid: str) -> None:
        raise RuntimeError("journal disk full")

    monkeypatch.setattr(TradeJournal, "record", boom)
    base = _run(config())
    assert base.closed_trades()
    assert base.journal == []  # nothing journaled, but the trades are identical to a normal run
    monkeypatch.undo()
    normal = _run(config())
    key = [(t.strategy_id, t.entry_time, t.exit_time, t.net_pnl) for t in base.closed_trades()]
    assert key == [(t.strategy_id, t.entry_time, t.exit_time, t.net_pnl) for t in normal.closed_trades()]


# --------------------------------------------------------------------------- #
# Static guards
# --------------------------------------------------------------------------- #


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return out


TRADING_ACTIONS = {"submit", "place_order", "cancel", "cancel_confirmed", "apply_advice", "request_exit", "trigger",
                   "reset", "open_trade", "exit_trade", "set_status", "record_trade_close", "record_entry"}


def test_the_recorder_and_journal_never_act_on_trading() -> None:
    for rel in ("runtime/learning.py", "learning/trade_journal.py"):
        used = _calls(PROJECT_ROOT / rel) & TRADING_ACTIONS
        assert not used, f"{rel} calls {used}"


def test_learning_never_imports_broker_execution_or_risk() -> None:
    for path in sorted((PROJECT_ROOT / "learning").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tops = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                tops.add(node.module.split(".")[0])
        assert not tops & {"broker", "execution", "risk", "runtime", "dashboard"}, path.name
