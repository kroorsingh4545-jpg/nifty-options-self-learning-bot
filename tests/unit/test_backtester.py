"""Event-driven backtester on SYNTHETIC data: replay, clock, no lookahead,
risk compliance, determinism and configuration guards."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from backtest.backtester import (
    BacktestConfigError,
    Backtester,
    BacktestOptions,
    BacktestResult,
    DayReplay,
    SimClock,
    SimulatedScheduler,
    candle_path,
    run_backtest,
    settle_simulation,
    with_settings_overrides,
    with_strategy_overrides,
)
from backtest.cost_model import CostModel
from backtest.data import BacktestDataError, HistoricalDataset
from backtest.synthetic_data import INDEX_KEY, SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from config.settings import BotConfig
from core.clock import IST
from core.models import Candle
from core.scheduler import TradingScheduler

SPEC = SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=3, seed=14)


@pytest.fixture(scope="module")
def cfg() -> BotConfig:
    return load_config(use_default_env_file=False)


@pytest.fixture(scope="module")
def dataset(cfg: BotConfig) -> HistoricalDataset:
    return generate_synthetic_dataset(SPEC, cfg.settings.market_session)


@pytest.fixture(scope="module")
def result(cfg: BotConfig, dataset: HistoricalDataset) -> BacktestResult:
    return run_backtest(cfg, dataset)


def _c(start: datetime, o: float, h: float, low: float, c: float, v: int = 300, key: str = INDEX_KEY) -> Candle:
    return Candle(instrument_key=key, timeframe_minutes=1, start=start, end=start + timedelta(minutes=1), open=o,
                  high=h, low=low, close=c, volume=v)


# --------------------------------------------------------------------------- #
# Replay and simulated time
# --------------------------------------------------------------------------- #


def test_candle_path_visits_the_adverse_extreme_first() -> None:
    t = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    assert candle_path(_c(t, 100, 110, 95, 108)) == (100, 95, 110, 108)  # up-candle: low first
    assert candle_path(_c(t, 100, 104, 90, 92)) == (100, 104, 90, 92)  # down-candle: high first


def test_day_replay_ticks_stay_inside_their_candle(cfg: BotConfig) -> None:
    t = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    candles = {INDEX_KEY: (_c(t, 100, 110, 95, 108, v=0), _c(t + timedelta(minutes=1), 108, 109, 101, 102, v=0))}
    replay = DayReplay(candles, {}, CostModel(cfg.settings.costs, slippage_ticks=1))
    assert replay.steps[0] == t and replay.steps[-1] == t + timedelta(minutes=1, seconds=45)
    assert all(a < b for a, b in zip(replay.steps, replay.steps[1:], strict=False))
    prices = [replay.ticks(s, {INDEX_KEY})[0].ltp for s in replay.steps]
    assert prices == [100, 95, 110, 108, 108, 109, 101, 102]
    for s in replay.steps:
        (tick,) = replay.ticks(s, {INDEX_KEY})
        assert tick.exchange_time == tick.received_at == s
        assert s - s.replace(second=0) < timedelta(minutes=1)  # never a price from the next minute


def test_option_ticks_carry_quotes_cumulative_volume_and_only_subscribed_keys(dataset: HistoricalDataset,
                                                                              cfg: BotConfig) -> None:
    d = dataset.days()[0]
    insts = {i.instrument_key: i for i in dataset.instruments}
    replay = DayReplay(dataset.day(d), insts, CostModel(cfg.settings.costs, slippage_ticks=1))
    opts = [k for k in dataset.day(d) if insts.get(k) and insts[k].option_type is not None]
    opt = max(opts, key=lambda k: dataset.day(d)[k][0].volume)  # near the money (well above the price floor)
    first = dataset.day(d)[opt][0]
    t0 = first.start
    ticks = [replay.ticks(t0 + timedelta(seconds=s), {opt, INDEX_KEY}) for s in (0, 15, 30, 45)]
    assert [x.instrument_key for x in ticks[0]] == [opt, INDEX_KEY]  # options before the index
    o = [x[0] for x in ticks]
    assert all(x.bid is not None and x.ask is not None and x.bid < x.ltp < x.ask for x in o)
    assert o[-1].volume_today == first.volume and o[0].volume_today == 0
    assert replay.ticks(t0, set()) == []


def test_sim_clock_never_goes_backwards() -> None:
    t = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    clock = SimClock(t)
    clock.set(t + timedelta(seconds=15))
    assert clock() == t + timedelta(seconds=15)
    with pytest.raises(RuntimeError, match="backwards"):
        clock.set(t)


async def test_simulated_scheduler_runs_interval_and_daily_jobs_on_sim_time() -> None:
    sch = TradingScheduler()
    calls: Counter[str] = Counter()
    sch.every("fast", 5, lambda: calls.update(["fast"]))
    sch.every("control", 1, lambda: calls.update(["control"]))
    sch.daily("sod", time(9, 0), lambda: calls.update(["sod"]))
    sim = SimulatedScheduler(sch)
    t = datetime(2026, 9, 7, 9, 0, tzinfo=IST)  # Monday

    async def tick(at: datetime) -> None:
        await asyncio.gather(*await sim.run_due(at))

    for s in range(0, 60, 15):
        await tick(t + timedelta(seconds=s))
    assert calls == Counter({"fast": 4, "sod": 1})  # control plane skipped; daily once
    await tick(t + timedelta(days=5))  # Saturday: no daily job
    assert calls["sod"] == 1
    await tick(t + timedelta(days=7))
    assert calls["sod"] == 2


async def test_a_slow_job_is_never_overlapped_and_does_not_block_the_replay() -> None:
    t = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    clock = SimClock(t)
    sch = TradingScheduler()
    runs: list[datetime] = []

    async def slow() -> None:
        runs.append(clock.now)
        await clock.sleep(40)  # e.g. a square-off whose exit is retrying

    sch.every("slow", 5, slow)
    sim = SimulatedScheduler(sch)
    for s in range(0, 90, 15):
        clock.set(t + timedelta(seconds=s))
        await sim.run_due(clock.now)  # returns at once: the job runs as a task
        await settle_simulation()
    # the 1st run wakes at +45 s and finishes in that step; the 2nd starts on the next step (max_instances=1)
    assert runs == [t, t + timedelta(seconds=60)]


async def test_sim_clock_sleep_parks_until_simulated_time_passes() -> None:
    t = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    clock = SimClock(t)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((name, clock.now))

    tasks = [asyncio.create_task(sleeper("a", 10)), asyncio.create_task(sleeper("b", 2))]
    await settle_simulation()
    assert woke == [] and clock.sleepers == 2  # no wall-clock time makes them run
    clock.set(t + timedelta(seconds=5))
    await settle_simulation()
    assert woke == [("b", t + timedelta(seconds=5))] and clock.sleepers == 1
    clock.set(t + timedelta(seconds=15))
    await asyncio.gather(*tasks)
    assert woke[-1] == ("a", t + timedelta(seconds=15))
    cancelled = asyncio.create_task(sleeper("c", 60))
    await settle_simulation()
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    assert clock.sleepers == 0  # a cancelled sleeper leaves nothing behind
    clock.close()
    await sleeper("d", 3600)  # after the run ends, sleeps return at once
    assert woke[-1][0] == "d"


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def test_backtest_trades_both_strategies_through_the_real_pipeline(result: BacktestResult) -> None:
    closed = result.closed_trades()
    assert {t.strategy_id for t in closed} == {"orb_options", "vwap_ema"}
    reasons = Counter(t.exit_reason for t in closed)
    assert reasons["TARGET"] >= 1 and reasons["SQUARE_OFF"] >= 1 and reasons["TRAILING_STOP"] >= 1
    assert result.data_source == "SYNTHETIC"
    for t in closed:
        assert t.entry_time is not None and t.exit_time is not None and t.net_pnl is not None
        assert t.created_at <= t.entry_time <= t.exit_time
        assert t.created_at.date() in result.days and t.exit_time.date() == t.entry_time.date()  # intraday only
        assert t.net_pnl == pytest.approx((t.gross_pnl or 0) - t.charges, abs=0.02)
        assert t.charges > 0
    assert not result.observations.open_after_close
    assert sum(result.observations.approved.values()) == len([t for t in result.trades if t.entry_reason])


def test_fills_and_executions_line_up(result: BacktestResult) -> None:
    assert len(result.executions) == len(result.fills)
    assert sum(e.charges for e in result.executions) == pytest.approx(sum(f.charges for f in result.fills), abs=0.05)
    buys = [e for e in result.executions if e.side.value == "BUY"]
    assert buys and all(e.price >= e.mid for e in buys)  # buys pay at or above the replayed price
    sells = [e for e in result.executions if e.side.value == "SELL"]
    assert sells and all(e.price <= e.mid for e in sells)


def test_no_entries_after_cutoff_and_flat_by_square_off(result: BacktestResult, cfg: BotConfig) -> None:
    rules = cfg.risk.time_rules
    for t in result.trades:
        assert t.created_at.time() < rules.no_new_entries_after
        if t.exit_time is not None:
            assert t.exit_time.time() <= rules.square_off_end
    sq = [t for t in result.closed_trades() if t.exit_reason == "SQUARE_OFF"]
    assert all(rules.square_off_start <= t.exit_time.time() for t in sq if t.exit_time)


def test_backtest_is_deterministic(cfg: BotConfig, dataset: HistoricalDataset, result: BacktestResult) -> None:
    again = run_backtest(cfg, dataset.slice(dataset.days()[:2]))

    def key(r: BacktestResult) -> list[tuple[object, ...]]:
        return [(t.strategy_id, t.trading_symbol, t.created_at, t.entry_price, t.exit_time, t.exit_price, t.net_pnl)
                for t in r.closed_trades() if t.created_at.date() in r.days]

    first_two = [k for k in key(result) if k[2].date() in again.days]  # type: ignore[attr-defined]
    assert key(again) == first_two


def _perturb_after(ds: HistoricalDataset, cutoff: datetime) -> HistoricalDataset:
    """Change every price from ``cutoff`` on (index/futures up 3%, options down 20%)."""
    insts = {i.instrument_key: i for i in ds.instruments}
    out: dict[str, tuple[Candle, ...]] = {}
    for key, series in ds.candles.items():
        inst = insts.get(key)
        f = 0.8 if inst is not None and inst.option_type is not None else 1.03
        tick = inst.tick_size if inst is not None else 0.01

        def rnd(x: float, tick: float = tick) -> float:
            return round(max(tick, round(x / tick) * tick), 2)

        out[key] = tuple(c if c.start < cutoff else c.model_copy(update={
            "open": rnd(c.open * f), "high": rnd(c.high * f), "low": rnd(c.low * f), "close": rnd(c.close * f)})
            for c in series)
    return HistoricalDataset(ds.instruments, out, ds.source, ds.description)


def test_no_lookahead_future_prices_cannot_change_past_decisions(cfg: BotConfig, dataset: HistoricalDataset,
                                                                 result: BacktestResult) -> None:
    """Rewrite every price after a cutoff: every signal, entry and exit
    before the cutoff must be identical."""
    days = dataset.days()
    cutoff = datetime.combine(days[1], time(12, 0), tzinfo=IST)
    changed = run_backtest(cfg, _perturb_after(dataset, cutoff))

    def before(r: BacktestResult) -> list[tuple[object, ...]]:
        rows = []
        for t in r.trades:
            if t.created_at >= cutoff:
                continue
            exit_part = (t.exit_time, t.exit_price, t.exit_reason) if t.exit_time and t.exit_time < cutoff else None
            rows.append((t.strategy_id, t.trading_symbol, t.created_at, t.entry_time, t.entry_price, t.quantity,
                         t.initial_stop_loss, t.target, exit_part))
        return rows

    assert before(result) and before(changed) == before(result)
    after_a = [(t.trading_symbol, t.net_pnl) for t in result.trades if t.created_at >= cutoff]
    after_b = [(t.trading_symbol, t.net_pnl) for t in changed.trades if t.created_at >= cutoff]
    assert after_a != after_b  # the perturbation did matter afterwards (the test is not vacuous)


def test_strategy_subset_runs_only_that_strategy(cfg: BotConfig, dataset: HistoricalDataset) -> None:
    res = run_backtest(cfg, dataset.slice(dataset.days()[:1]), BacktestOptions(strategies=("orb_options",)))
    assert res.strategies == ["orb_options"] and set(res.capital) == {"orb_options"}
    assert {t.strategy_id for t in res.trades} <= {"orb_options"}
    assert res.observations.signals.get("vwap_ema", 0) == 0


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def test_year_without_holidays_is_refused(cfg: BotConfig) -> None:
    ds = generate_synthetic_dataset(replace(SPEC, start=date(2027, 1, 4), days=1))
    with pytest.raises(BacktestDataError, match="no NSE holidays configured for 2027"):
        Backtester(cfg, ds)


def test_data_on_a_configured_holiday_is_refused(cfg: BotConfig, dataset: HistoricalDataset) -> None:
    holiday = with_settings_overrides(cfg, {"market_session": {"holidays": [
        *cfg.settings.market_session.holidays, dataset.days()[0]]}})
    with pytest.raises(BacktestDataError, match="not an NSE trading day"):
        Backtester(holiday, dataset)


def test_trading_database_and_existing_files_are_refused(cfg: BotConfig, dataset: HistoricalDataset,
                                                         tmp_path: Path) -> None:
    with pytest.raises(BacktestConfigError, match="trading database"):
        Backtester(cfg, dataset, BacktestOptions(database_path=cfg.database_path))._database()
    existing = tmp_path / "x.db"
    existing.write_text("")
    with pytest.raises(BacktestConfigError, match="already exists"):
        Backtester(cfg, dataset, BacktestOptions(database_path=existing))._database()


@pytest.mark.parametrize("override", [
    {"orb_options": {"allocated_capital": 150_000}},
    {"orb_options": {"risk": {"max_daily_loss": 9_000}}},
    {"orb_options": {"sizing": {"risk_per_trade": 1_000}}},
    {"orb_options": {"ml": {"confidence_threshold": 0.1}}},
    {"nope": {"params": {}}},
])
def test_capital_and_risk_overrides_are_refused(cfg: BotConfig, override: dict[str, dict[str, object]]) -> None:
    with pytest.raises(BacktestConfigError):
        with_strategy_overrides(cfg, override)


def test_overrides_are_revalidated(cfg: BotConfig) -> None:
    ok = with_strategy_overrides(cfg, {"orb_options": {"params": {"breakout_buffer_points": 10},
                                                       "sizing": {"size_multiplier": 0.5}}})
    s = ok.strategies.strategies["orb_options"]
    assert s.params["breakout_buffer_points"] == 10 and s.sizing.size_multiplier == 0.5
    assert s.sizing.risk_per_trade == cfg.strategies.strategies["orb_options"].sizing.risk_per_trade
    with pytest.raises(BacktestConfigError):  # entry after the 14:45 cutoff fails the hard-limit validator
        with_strategy_overrides(cfg, {"orb_options": {"entry_window": {"end": "15:00"}}})
    with pytest.raises(BacktestConfigError):  # size multiplier can only reduce
        with_strategy_overrides(cfg, {"orb_options": {"sizing": {"size_multiplier": 2.0}}})
    with pytest.raises(BacktestConfigError):
        with_settings_overrides(cfg, {"paper_broker": {"slippage_ticks": 99}})
    with pytest.raises(BacktestConfigError):
        with_settings_overrides(cfg, {"broker": {"rest_base_url": "https://example.com"}})
    assert with_settings_overrides(cfg, {"paper_broker": {"slippage_ticks": 2}}).settings.paper_broker \
        .slippage_ticks == 2


def test_higher_slippage_costs_more(cfg: BotConfig, dataset: HistoricalDataset) -> None:
    one = dataset.slice(dataset.days()[:1])
    base = run_backtest(cfg, one)
    worse = run_backtest(with_settings_overrides(cfg, {"paper_broker": {"slippage_ticks": 4}}), one)
    a = base.cost_model.attribute(base.executions)
    b = worse.cost_model.attribute(worse.executions)
    assert base.executions and worse.executions
    assert b.slippage / sum(e.quantity for e in worse.executions) > \
        a.slippage / sum(e.quantity for e in base.executions)


def test_dataset_must_contain_the_index(cfg: BotConfig, dataset: HistoricalDataset) -> None:
    no_index = HistoricalDataset(dataset.instruments, {k: v for k, v in dataset.candles.items() if k != INDEX_KEY},
                                 dataset.source)
    with pytest.raises(BacktestDataError, match="required instrument"):
        Backtester(cfg, no_index)
