"""Backtest metrics (hand-computed), risk-compliance checks, and the report format."""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from backtest.__main__ import main
from backtest.backtester import BacktestResult, run_backtest
from backtest.metrics import compliance, compliance_status, daily_returns, max_drawdown, performance
from backtest.report_generator import SYNTHETIC_BANNER, TRADE_COLUMNS, build_report, render_markdown, write_report
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from core.clock import IST
from core.enums import OptionType, OrderSide, TradeStatus, TradingMode
from data.storage.records import TradeRecord
from tests.fakes import with_per_trade_risk

CFG = with_per_trade_risk(load_config(use_default_env_file=False), 2_000)  # losses measured against Rs 2,000
D1, D2, D3 = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)


def trade(n: int, day: date, net: float, *, entry: time = time(10, 0), minutes: int = 30, sid: str = "orb_options",
          r: float | None = None, charges: float = 50.0) -> TradeRecord:
    start = datetime.combine(day, entry, tzinfo=IST)
    return TradeRecord(
        id=n, trade_uid=f"PAPER-T-{sid}-{n}", mode=TradingMode.PAPER, strategy_id=sid, strategy_version="0.1.0",
        instrument_key="NSE_FO|1", trading_symbol="NIFTY X", option_type=OptionType.CE, strike=25000.0,
        expiry=day, side=OrderSide.BUY, quantity=65, entry_price=100.0, entry_time=start, exit_price=101.0,
        exit_time=start + timedelta(minutes=minutes), initial_stop_loss=75.0, stop_loss=75.0, target=150.0,
        gross_pnl=net + charges, charges=charges, net_pnl=net, r_multiple=r, status=TradeStatus.CLOSED,
        exit_reason="TARGET" if net > 0 else "STOP_LOSS", created_at=start, updated_at=start)


TRADES = [trade(1, D1, 1000.0, r=1.0), trade(2, D1, -500.0, r=-0.5), trade(3, D2, -1500.0, minutes=60, r=-1.2),
          trade(4, D3, 2500.0, minutes=90, r=2.0)]


def test_performance_metrics_match_hand_calculation() -> None:
    m = performance(TRADES, [D1, D2, D3], 100_000.0)
    assert (m.trades, m.wins, m.losses) == (4, 2, 2) and m.win_rate_pct == 50.0
    assert m.net_pnl == 1500.0 and m.charges == 200.0 and m.gross_pnl == 1700.0
    assert m.total_return_pct == 1.5
    assert m.profit_factor == pytest.approx(3500 / 2000)
    assert m.expectancy == 375.0 and m.avg_win == 1750.0 and m.avg_loss == -1000.0
    assert m.best_trade == 2500.0 and m.worst_trade == -1500.0
    assert m.avg_r_multiple == pytest.approx((1.0 - 0.5 - 1.2 + 2.0) / 4, abs=1e-3)
    assert m.avg_trade_minutes == pytest.approx((30 + 30 + 60 + 90) / 4)
    rets = [500 / 100_000, -1500 / 100_000, 2500 / 100_000]
    assert m.avg_daily_return_pct == pytest.approx(statistics.fmean(rets) * 100, abs=1e-4)
    sharpe = statistics.fmean(rets) / statistics.stdev(rets) * math.sqrt(252)
    assert m.sharpe == pytest.approx(sharpe, abs=1e-3)
    cagr = (math.prod(1 + x for x in rets) ** (252 / 3) - 1) * 100
    assert m.cagr_like_pct == pytest.approx(cagr, abs=0.01)
    # equity 100000 -> 101000 (peak) -> 100500 -> 99000 (trough) -> 101500
    assert m.max_drawdown == 2000.0 and m.max_drawdown_pct == pytest.approx(2000 / 101_000 * 100, abs=1e-3)
    assert m.exit_reasons == {"TARGET": 2, "STOP_LOSS": 2} and m.days_with_trades == 3


def test_days_without_trades_count_as_zero_return() -> None:
    assert daily_returns(TRADES, [D1, date(2026, 9, 10), D2], 100_000.0) == [0.005, 0.0, -0.015]


def test_edge_cases_without_losses_or_trades() -> None:
    only_wins = performance([trade(1, D1, 100.0)], [D1], 100_000.0)
    assert only_wins.profit_factor is None and only_wins.sharpe is None  # no losses / one day
    empty = performance([], [D1, D2], 100_000.0)
    assert empty.trades == 0 and empty.win_rate_pct is None and empty.net_pnl == 0 and empty.sharpe is None
    assert empty.max_drawdown == 0 and empty.cagr_like_pct == 0.0
    assert max_drawdown([], 100_000.0) == (0.0, 0.0)
    open_trade = trade(9, D1, 0.0).model_copy(update={"status": TradeStatus.OPEN})
    assert performance([open_trade], [D1], 100_000.0).trades == 0


def test_compliance_flags_breaches_and_overshoots() -> None:
    late = trade(5, D1, -100.0, entry=time(14, 50))  # after the 14:45 cutoff and outside the ORB window
    gap = trade(6, D2, -2600.0)  # lost more than the 2,000 per-trade budget
    bad_day = [trade(10 + i, D3, -1300.0, entry=time(10, i)) for i in range(4)]  # -5,200 > 5,000 limit
    many = [trade(20 + i, D1, 10.0, entry=time(10, 30 + i), sid="vwap_ema") for i in range(5)]  # 5 > 4 per day
    checks = compliance(CFG, [late, gap, *bad_day, *many], max_open={"orb_options": 3}, max_open_total=5,
                        open_after_close={"2026-09-07": 1})
    status = {c.name.split(" (")[0]: c.status for c in checks}
    assert status["open trades per strategy"] == "BREACH" and status["open trades total"] == "BREACH"
    assert status["trades per strategy per day"] == "BREACH"
    assert status["no entries at/after 14:45"] == "BREACH"
    assert status["entries inside the strategy entry window"] == "BREACH"
    assert status["flat after square-off"] == "BREACH"
    assert status["loss per trade within the per-trade risk budget"] == "WARN"
    assert status["daily loss per strategy within limit"] == "WARN"
    assert compliance_status(checks) == "FAIL"
    clean = compliance(CFG, TRADES, max_open={"orb_options": 1}, max_open_total=1, open_after_close={})
    assert compliance_status(clean) == "PASS" and all(c.status == "OK" for c in clean)


# --------------------------------------------------------------------------- #
# Report on a real (SYNTHETIC) run
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def result() -> BacktestResult:
    ds = generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=D1, days=2, seed=14),
                                    CFG.settings.market_session)
    return run_backtest(CFG, ds)


def test_report_contains_every_required_metric(result: BacktestResult, tmp_path: Path) -> None:
    report = build_report(result)
    assert report.synthetic and report.data_source == "SYNTHETIC" and report.trading_days == 2
    required = {"total_return_pct", "cagr_like_pct", "win_rate_pct", "profit_factor", "max_drawdown", "sharpe",
                "avg_trade_minutes", "trades", "net_pnl", "charges"}
    assert required <= set(report.summary)
    assert set(report.by_strategy) == {"orb_options", "vwap_ema"}
    assert {"brokerage", "stt", "exchange_txn", "gst", "sebi_fee", "stamp_duty", "slippage", "spread",
            "pnl_before_costs"} <= set(report.costs)
    assert report.costs["net_pnl"] == pytest.approx(report.summary["net_pnl"], abs=0.05)
    assert report.costs["charges"] == pytest.approx(report.summary["charges"], abs=0.5)
    assert report.compliance_status in ("PASS", "PASS WITH WARNINGS")
    assert len(report.trades) == len(result.trades) and all(set(t) == set(TRADE_COLUMNS) for t in report.trades)
    assert len(report.daily) == 2
    md = render_markdown(report)
    assert SYNTHETIC_BANNER in md
    for section in ("## Summary", "## Cost impact", "## Risk compliance", "## Signal pipeline", "## Daily net P&L",
                    "## Trades", "## Assumptions"):
        assert section in md
    paths = write_report(report, tmp_path / "out")
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["summary"]["trades"] == report.summary["trades"]
    rows = paths["trades"].read_text(encoding="utf-8").splitlines()
    assert rows[0].split(",")[:3] == list(TRADE_COLUMNS[:3]) and len(rows) == len(report.trades) + 1


def test_historical_report_has_no_synthetic_banner(result: BacktestResult) -> None:
    result_hist = BacktestResult(**{**result.__dict__, "data_source": "HISTORICAL"})
    assert SYNTHETIC_BANNER not in render_markdown(build_report(result_hist))


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
    root.setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_cli_synthetic_run_writes_a_report(tmp_path: Path) -> None:
    out = tmp_path / "cli"
    with pytest.raises(SystemExit, match="lot-units"):
        main(["synthetic", "--days", "1", "--quiet", "--out", str(out)])
    code = main(["synthetic", "--lot-units", "65", "--days", "1", "--start", "2026-09-07", "--seed", "14", "--quiet",
                 "--out", str(out),
                 "--strategies", "orb_options", "--save-data", str(tmp_path / "data")])
    assert code == 0
    assert (out / "report.md").is_file() and (out / "report.json").is_file() and (out / "trades.csv").is_file()
    assert (tmp_path / "data" / "candles.csv").is_file()
    code = main(["run", "--data", str(tmp_path / "data"), "--quiet", "--out", str(tmp_path / "cli2"),
                 "--strategies", "orb_options"])
    assert code == 0
    a = json.loads((out / "report.json").read_text(encoding="utf-8"))["summary"]
    b = json.loads((tmp_path / "cli2" / "report.json").read_text(encoding="utf-8"))["summary"]
    assert a == b  # the exported dataset replays to the same result
