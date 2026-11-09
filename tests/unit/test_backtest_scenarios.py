"""Backtest risk scenarios on SYNTHETIC data (Phase 6 audit, required tests 1-5):
a data gap at square-off, exit retries on simulated time, a futures roll,
partial fills, and a daily-loss kill carried into the next day."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from backtest.backtester import BacktestOptions, BacktestResult, run_backtest
from backtest.cost_model import SpreadModel
from backtest.data import HistoricalDataset
from backtest.report_generator import build_report
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from config.settings import BotConfig
from core.clock import IST
from core.enums import OrderPurpose, OrderSide, OrderStatus, OrderType
from execution.order_manager import OrderIntent
from risk.daily_loss_guard import DailyLossGuard
from tests.stack_harness import CE, Harness

CFG = load_config(use_default_env_file=False)
SESSION = CFG.settings.market_session
GAP_DAY, NEXT_DAY = date(2026, 9, 9), date(2026, 9, 10)


def _spec(**kw: Any) -> SyntheticSpec:
    base: dict[str, Any] = {"lot_units": 65, "start": date(2026, 9, 7), "days": 4, "seed": 14}
    base.update(kw)
    return SyntheticSpec(**base)


# --------------------------------------------------------------------------- #
# 1 + 2: a data gap at square-off; exit retries spread over simulated time
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def gap_runs() -> tuple[BacktestResult, BacktestResult, str]:
    ds = generate_synthetic_dataset(_spec(), SESSION)
    base = run_backtest(CFG, ds)
    held = next(t for t in base.closed_trades() if t.exit_reason == "SQUARE_OFF" and t.created_at.date() == GAP_DAY)
    cut = datetime.combine(GAP_DAY, time(15, 0), tzinfo=IST)
    candles = {k: v if k != held.instrument_key else tuple(c for c in v if c.start.date() != GAP_DAY or c.start < cut)
               for k, v in ds.candles.items()}
    gapped = run_backtest(CFG, HistoricalDataset(ds.instruments, candles, ds.source, ds.description))
    return base, gapped, held.instrument_key


def test_without_the_gap_the_trade_is_squared_off_intraday(gap_runs: tuple[BacktestResult, BacktestResult, str]) \
        -> None:
    base, _, key = gap_runs
    t = next(t for t in base.closed_trades() if t.instrument_key == key and t.created_at.date() == GAP_DAY)
    assert t.exit_time is not None and t.exit_time.date() == GAP_DAY and t.exit_time.time() >= time(15, 10)
    assert not base.observations.open_after_close and not base.observations.kill_switch_days


def test_data_gap_at_square_off_escalates_and_is_reported(gap_runs: tuple[BacktestResult, BacktestResult, str]) \
        -> None:
    _, gapped, key = gap_runs
    obs = gapped.observations
    assert obs.open_after_close == {GAP_DAY.isoformat(): 1}
    assert GAP_DAY.isoformat() in obs.kill_switch_days and NEXT_DAY.isoformat() in obs.kill_switch_days
    events = {e.event_type for e in gapped.risk_events}
    assert {"EXIT_FAILED", "KILL_SWITCH_TRIGGERED", "ORDER_REJECTED"} <= events
    # the kill is persistent: next morning every strategy is blocked and KILLED, and nothing new is opened
    assert all(obs.blocked_at_open[NEXT_DAY.isoformat()].values())
    assert set(obs.strategy_status[NEXT_DAY.isoformat()].values()) == {"KILLED"}
    assert not [t for t in gapped.trades if t.created_at.date() == NEXT_DAY]
    # the stranded position is still protected and closes once data returns the next day
    stranded = next(t for t in gapped.trades if t.instrument_key == key and t.created_at.date() == GAP_DAY)
    assert stranded.exit_time is not None and stranded.exit_time.date() == NEXT_DAY
    report = build_report(gapped)
    assert report.compliance_status == "FAIL"
    flat = next(c for c in report.compliance if c["name"] == "flat after square-off")
    assert flat["status"] == "BREACH" and GAP_DAY.isoformat() in flat["detail"]
    assert any("still open after the close" in w for w in report.warnings)


def test_exit_retries_run_on_simulated_time(gap_runs: tuple[BacktestResult, BacktestResult, str]) -> None:
    """Warning 1 fix: retries/backoffs wait for simulated time, so each
    attempt meets a later tick (or the lack of one), never all at one instant."""
    _, gapped, key = gap_runs
    exits = sorted(o.created_at for o in gapped.orders
                   if o.instrument_key == key and o.side is OrderSide.SELL and o.created_at.date() == GAP_DAY
                   and o.purpose in (OrderPurpose.SQUARE_OFF, OrderPurpose.KILL_SWITCH, OrderPurpose.EXIT))
    assert len(exits) >= 5
    assert len(set(exits)) == len(exits)  # one attempt per instant
    assert all(t.time() >= time(15, 10) for t in exits)
    assert exits[-1] - exits[0] >= timedelta(minutes=1)
    rejected = [o for o in gapped.orders if o.instrument_key == key and o.status is OrderStatus.REJECTED]
    assert rejected and all("no fresh market data" in (o.status_message or "") for o in rejected)


async def test_wait_terminal_times_out_on_the_injected_clock() -> None:
    h = Harness()
    waited: list[float] = []
    release = asyncio.Event()

    async def sim_sleep(seconds: float) -> None:
        waited.append(seconds)
        await release.wait()

    h.s.orders._sleep = sim_sleep
    h.tick(CE.instrument_key, 100.0)
    rec = await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, CE.lot_size, OrderType.LIMIT,
                                              OrderPurpose.ENTRY, price=50.0))  # far below the ask: rests
    waiting = asyncio.create_task(h.s.orders.wait_terminal(rec.client_order_id, 7.0))
    await asyncio.sleep(0.05)  # real time passes; only the injected clock may end the wait
    assert not waiting.done() and waited == [7.0]
    release.set()
    still_open = await waiting
    assert still_open is not None and still_open.status is OrderStatus.OPEN
    # an order that finishes first ends the wait at once, whatever the clock says
    release.clear()
    waiting = asyncio.create_task(h.s.orders.wait_terminal(rec.client_order_id, 7.0))
    await asyncio.sleep(0)
    await h.s.orders.cancel(rec.client_order_id)
    done = await asyncio.wait_for(waiting, 1.0)
    assert done is not None and done.status is OrderStatus.CANCELLED


# --------------------------------------------------------------------------- #
# 3: crossing a monthly futures expiry
# --------------------------------------------------------------------------- #


def test_futures_roll_moves_vwap_ema_to_the_next_contract() -> None:
    ds = generate_synthetic_dataset(_spec(start=date(2026, 9, 28), seed=5), SESSION)
    futs = sorted((i for i in ds.instruments if i.instrument_type == "FUT"), key=lambda i: i.expiry or date.max)
    sep, octo = futs[0], futs[1]
    assert sep.expiry == date(2026, 9, 29) and octo.expiry == date(2026, 10, 27)
    res = run_backtest(CFG, ds)
    obs = res.observations
    assert obs.futures_by_day["2026-09-28"] == obs.futures_by_day["2026-09-29"] == sep.instrument_key  # expiry day
    assert obs.futures_by_day["2026-09-30"] == obs.futures_by_day["2026-10-01"] == octo.instrument_key
    assert obs.signal_instruments["2026-09-29"]["vwap_ema"] == sep.instrument_key
    assert obs.signal_instruments["2026-09-30"]["vwap_ema"] == octo.instrument_key
    assert {d["orb_options"] for d in obs.signal_instruments.values()} == {CFG.settings.instruments.nifty_index_key}
    assert not res.warnings and build_report(res).compliance_status != "FAIL"


# --------------------------------------------------------------------------- #
# 4: depth smaller than the order: partial fills in the replay
# --------------------------------------------------------------------------- #


def test_partial_fills_keep_positions_and_pnl_consistent() -> None:
    lot = 25
    ds = generate_synthetic_dataset(_spec(lot_units=lot, days=1), SESSION)
    res = run_backtest(CFG, ds, BacktestOptions(spread=SpreadModel(depth_lots=1)))
    by_order = {o.id: o for o in res.orders}
    fills_per_order: dict[int, int] = defaultdict(int)
    qty: dict[tuple[str, OrderSide], int] = defaultdict(int)
    charges: dict[str, float] = defaultdict(float)
    for f in res.fills:
        o = by_order[f.order_id]
        assert f.quantity <= lot  # never more than the one lot on offer per tick
        fills_per_order[f.order_id] += 1
        assert o.trade_uid is not None
        qty[(o.trade_uid, o.side)] += f.quantity
        charges[o.trade_uid] += f.charges
    assert max(fills_per_order.values()) >= 2, "fixture must produce partial fills"
    closed = res.closed_trades()
    assert closed and any(t.quantity > lot for t in closed)
    for t in closed:
        assert t.quantity % lot == 0
        assert qty[(t.trade_uid, OrderSide.BUY)] == qty[(t.trade_uid, OrderSide.SELL)] == t.quantity
        assert t.charges == pytest.approx(charges[t.trade_uid], abs=0.05)
        assert t.net_pnl == pytest.approx((t.gross_pnl or 0) - t.charges, abs=0.02)
    assert not res.observations.open_after_close


# --------------------------------------------------------------------------- #
# 5: a daily-loss breach: kill for the day, re-activated the next day
# --------------------------------------------------------------------------- #


def _tight_limits_config(config_dir: Path,
                         edit_yaml: Callable[[str, Callable[[dict[str, Any]], None]], None]) -> BotConfig:
    def risk(d: dict[str, Any]) -> None:  # stricter limits only (hard limits allow tightening)
        d["per_strategy"]["max_daily_loss"] = 1000
        d["per_trade"]["max_risk_per_trade"] = 1000

    def strategies(d: dict[str, Any]) -> None:
        for s in d["strategies"].values():
            s["risk"]["max_daily_loss"] = 1000
            s["sizing"]["risk_per_trade"] = 1000

    edit_yaml("risk_limits.yaml", risk)
    edit_yaml("strategy_defaults.yaml", strategies)
    return load_config(config_dir, use_default_env_file=False)


def test_stop_outs_inside_the_daily_limit_do_not_kill(
        config_dir: Path, edit_yaml: Callable[[str, Callable[[dict[str, Any]], None]], None]) -> None:
    """Real losses of 788.77 and 625.83 against a 1,000 limit. Before the
    close-order fix the guard counted the closing trade twice (once realized,
    once still open), saw 1,363.77 and 1,074.58 and fired the kill switch."""
    cfg = _tight_limits_config(config_dir, edit_yaml)
    res = run_backtest(cfg, generate_synthetic_dataset(_spec(lot_units=25), SESSION))
    day = [t for t in res.closed_trades() if t.created_at.date() == GAP_DAY]
    assert sorted(t.exit_reason or "" for t in day) == ["STOP_LOSS", "STOP_LOSS"]
    assert all(-1000 < (t.net_pnl or 0) < 0 for t in day)
    assert not [e for e in res.risk_events if e.event_type == "KILL_SWITCH_TRIGGERED"]


def test_daily_loss_kill_blocks_the_day_and_clears_next_morning(
        config_dir: Path, edit_yaml: Callable[[str, Callable[[dict[str, Any]], None]], None],
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Sizing normally keeps one trade inside the day's limit; a loss worse than
    # planned (a gap through the stop) is what breaches it. The guard's limit is
    # set below what the trades were sized for, so one real loss goes through it.
    monkeypatch.setattr(DailyLossGuard, "remaining_risk_budget", lambda self, sid: 1000.0)
    monkeypatch.setattr(DailyLossGuard, "_limit", lambda self, sid: 600.0)
    cfg = _tight_limits_config(config_dir, edit_yaml)
    res = run_backtest(cfg, generate_synthetic_dataset(_spec(lot_units=25), SESSION))

    kills = {e.strategy_id: e for e in res.risk_events if e.event_type == "KILL_SWITCH_TRIGGERED"}
    assert set(kills) == {"orb_options", "vwap_ema"}
    for sid, e in kills.items():
        assert e.created_at.date() == GAP_DAY and e.details_json["reason"] == "STRATEGY_DAILY_LOSS"
        # nothing new for that strategy for the rest of the day
        assert not [t for t in res.trades if t.strategy_id == sid and t.created_at.date() == GAP_DAY
                    and t.created_at > e.created_at]
    assert any("kill switch" in reason for reason in res.observations.rejections["vwap_ema"])
    resets = [e for e in res.risk_events if e.event_type == "KILL_SWITCH_RESET"]
    assert {e.strategy_id for e in resets} == {"orb_options", "vwap_ema"}
    assert all(e.created_at == datetime.combine(NEXT_DAY, time(9, 0), tzinfo=IST) for e in resets)  # simulated time
    obs = res.observations
    assert set(obs.strategy_status[NEXT_DAY.isoformat()].values()) == {"ACTIVE"}
    assert not any(obs.blocked_at_open[NEXT_DAY.isoformat()].values())
    assert {t.strategy_id for t in res.trades if t.created_at.date() == NEXT_DAY} == {"orb_options", "vwap_ema"}
    assert not obs.kill_switch_days  # strategy kills only: the global switch never fired
