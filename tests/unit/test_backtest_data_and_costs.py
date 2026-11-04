"""Backtest dataset format/validation, the SYNTHETIC generator, and the cost model."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from backtest.cost_model import CostModel, Execution, SpreadModel
from backtest.data import BacktestDataError, HistoricalDataset, load_dataset, save_dataset
from backtest.synthetic_data import (
    INDEX_KEY,
    SyntheticSpec,
    black_scholes,
    generate_synthetic_dataset,
    monthly_expiry,
    trading_days,
    weekly_expiry,
)
from broker.charges import ChargeCalculator
from config.loader import load_config
from config.settings import CostsSection, MarketSession
from core.clock import IST
from core.enums import OrderSide
from core.models import Candle

SESSION = load_config(use_default_env_file=False).settings.market_session
OPEN, CLOSE = SESSION.market_open, SESSION.market_close


@pytest.fixture(scope="module")
def small() -> HistoricalDataset:
    return generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 11, 6), days=2, seed=3), SESSION)


def _c(key: str, start: datetime, tf: int = 1) -> Candle:
    return Candle(instrument_key=key, timeframe_minutes=tf, start=start, end=start + timedelta(minutes=tf), open=10,
                  high=11, low=9, close=10)


# --------------------------------------------------------------------------- #
# SYNTHETIC generator
# --------------------------------------------------------------------------- #


def test_synthetic_dataset_is_labelled_and_deterministic(small: HistoricalDataset) -> None:
    assert small.source == "SYNTHETIC" and small.is_synthetic and "SYNTHETIC" in small.description
    assert all("SYNTH" in i.trading_symbol for i in small.instruments)
    again = generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 11, 6), days=2, seed=3), SESSION)
    assert again.candles[INDEX_KEY] == small.candles[INDEX_KEY]
    other = generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 11, 6), days=2, seed=4), SESSION)
    assert other.candles[INDEX_KEY] != small.candles[INDEX_KEY]
    small.validate(session_open=OPEN, session_close=CLOSE, required_keys=(INDEX_KEY,))


def test_synthetic_calendar_skips_weekends_and_holidays(small: HistoricalDataset) -> None:
    # 2026-11-06 Fri, then the weekend and the 2026-11-10 Diwali holiday
    assert trading_days(date(2026, 11, 6), 3, SESSION) == [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 11)]
    assert small.days() == [date(2026, 11, 6), date(2026, 11, 9)]
    # the Tuesday weekly expiry falls on the Diwali holiday -> moves to Monday 2026-11-09
    assert weekly_expiry(date(2026, 11, 6), SESSION) == date(2026, 11, 9)
    assert weekly_expiry(date(2026, 11, 11), SESSION) == date(2026, 11, 17)
    assert monthly_expiry(2026, 9, SESSION) == date(2026, 9, 29)
    assert monthly_expiry(2026, 11, SESSION) == date(2026, 11, 23)  # Tue 24 Nov is a holiday -> Monday 23rd
    opts = [i for i in small.instruments if i.option_type is not None]
    assert {i.expiry for i in opts} == {date(2026, 11, 9)}
    assert {i.lot_size for i in small.instruments} == {65}  # the caller's lot_units, written into the list
    other = generate_synthetic_dataset(SyntheticSpec(lot_units=40, start=date(2026, 11, 6), days=1), SESSION)
    assert {i.lot_size for i in other.instruments} == {40}


def test_synthetic_spec_has_no_default_contract_size() -> None:
    with pytest.raises(TypeError):
        SyntheticSpec()  # type: ignore[call-arg]


def test_synthetic_option_prices_track_black_scholes(small: HistoricalDataset) -> None:
    assert black_scholes(100, 100, 0.1, 0.2, 0.0, True) == pytest.approx(2.52, abs=0.01)
    call, put = black_scholes(100, 95, 0.1, 0.2, 0.05, True), black_scholes(100, 95, 0.1, 0.2, 0.05, False)
    assert call - put == pytest.approx(100 - 95 * 2.718281828 ** (-0.005), abs=1e-6)  # put-call parity
    assert black_scholes(100, 90, 0.0, 0.2, 0.0, True) == 10 and black_scholes(100, 90, 0.0, 0.2, 0.0, False) == 0
    d = small.days()[0]
    spot = small.day(d)[INDEX_KEY][0].close
    by_strike = sorted((i for i in small.instruments if i.instrument_type == "CE"), key=lambda i: i.strike or 0)
    prices = [small.day(d)[i.instrument_key][0].close for i in by_strike]
    assert prices == sorted(prices, reverse=True)  # calls get cheaper with strike
    atm = min(by_strike, key=lambda i: abs((i.strike or 0) - spot))
    assert 20 < small.day(d)[atm.instrument_key][0].close < 400


# --------------------------------------------------------------------------- #
# Validation, loading and saving
# --------------------------------------------------------------------------- #


def test_save_and_load_round_trip(small: HistoricalDataset, tmp_path: Path) -> None:
    loaded = load_dataset(save_dataset(small, tmp_path / "ds"))
    assert loaded.source == "SYNTHETIC" and loaded.description == small.description
    assert {i.instrument_key: i for i in loaded.instruments} == {i.instrument_key: i for i in small.instruments}
    assert loaded.candles[INDEX_KEY] == small.candles[INDEX_KEY]
    assert loaded.days() == small.days()


def test_naive_timestamps_and_bad_meta_are_refused(small: HistoricalDataset, tmp_path: Path) -> None:
    d = save_dataset(small.slice(small.days()[:1]), tmp_path / "ds")
    csv_path = d / "candles.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("+05:30", "")
    csv_path.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(BacktestDataError, match="no UTC offset"):
        load_dataset(d)
    (d / "meta.json").write_text(json.dumps({"source": "LIVE"}), encoding="utf-8")
    with pytest.raises(BacktestDataError):
        load_dataset(d)


@pytest.mark.parametrize(("bad", "match"), [
    ("tf", "1-minute"), ("seconds", "whole minute"), ("session", "outside"), ("order", "strictly increasing"),
    ("unknown", "not in the instrument list"), ("incomplete", "incomplete"),
])
def test_validation_rejects_corrupt_candles(small: HistoricalDataset, bad: str, match: str) -> None:
    t = datetime(2026, 11, 6, 9, 15, tzinfo=IST)
    key = INDEX_KEY
    series = {"tf": (_c(key, t, tf=5),), "seconds": (_c(key, t + timedelta(seconds=5)),),
              "session": (_c(key, datetime(2026, 11, 6, 15, 30, tzinfo=IST)),),
              "order": (_c(key, t + timedelta(minutes=1)), _c(key, t)),
              "unknown": (_c(key, t),), "incomplete": (_c(key, t).model_copy(update={"complete": False}),)}[bad]
    candles = {key: series} if bad != "unknown" else {key: series, "NSE_FO|NOPE": (_c("NSE_FO|NOPE", t),)}
    ds = HistoricalDataset(small.instruments, candles)
    with pytest.raises(BacktestDataError, match=match):
        ds.validate(session_open=OPEN, session_close=CLOSE, required_keys=(INDEX_KEY,))


def test_slice_only_keeps_the_requested_days(small: HistoricalDataset) -> None:
    first = small.days()[0]
    s = small.slice([first])
    assert s.days() == [first]
    assert all(c.start.date() == first for series in s.candles.values() for c in series)
    assert s.instruments == small.instruments and s.source == small.source


def test_dataset_without_options_is_refused() -> None:
    t = datetime(2026, 11, 6, 9, 15, tzinfo=IST)
    with pytest.raises(BacktestDataError, match="no options"):
        HistoricalDataset((), {INDEX_KEY: (_c(INDEX_KEY, t),)}).validate(session_open=time(9, 15),
                                                                        session_close=time(15, 30))


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #


def test_quote_builds_a_tick_aligned_spread() -> None:
    m = CostModel(CostsSection(), slippage_ticks=1, spread=SpreadModel(half_spread_pct=0.25, min_half_spread_ticks=1))
    bid, ask = m.quote(100.0, 0.05)
    assert (bid, ask) == (99.75, 100.25)
    bid, ask = m.quote(10.0, 0.05)  # 0.25% < one tick -> one tick each side
    assert (bid, ask) == (9.95, 10.05)
    bid, ask = m.quote(0.05, 0.05)  # never a zero or negative bid
    assert bid == 0.05 and ask > bid
    assert m.depth(65) == 20 * 65


def test_attribution_matches_the_charge_calculator_and_splits_execution_costs() -> None:
    costs = CostsSection()
    m = CostModel(costs, slippage_ticks=1)
    calc = ChargeCalculator(costs)
    ex = [
        Execution("orb_options", "O1", OrderSide.BUY, 65, 100.30, 100.0, 0.0, 0.05, True),
        Execution("orb_options", "O2", OrderSide.SELL, 65, 119.70, 120.0, 0.0, 0.05, True),
        Execution("orb_options", "O2", OrderSide.SELL, 65, 119.70, 120.0, 0.0, 0.05, False),  # 2nd fill: no brokerage
    ]
    a = m.attribute(ex)
    expected = calc.compute(OrderSide.BUY, 65, 100.30).total + calc.compute(OrderSide.SELL, 65, 119.70).total + \
        calc.compute(OrderSide.SELL, 65, 119.70, include_brokerage=False).total
    assert a.charges == pytest.approx(expected, abs=0.02)
    assert a.brokerage == 2 * costs.brokerage_per_order
    assert a.stt > 0 and a.stamp_duty > 0 and a.gst > 0 and a.exchange_txn > 0 and a.sebi_fee > 0
    assert a.slippage == pytest.approx(3 * 65 * 0.05)
    assert a.spread == pytest.approx(3 * 65 * 0.30 - a.slippage)
    assert a.total == pytest.approx(a.charges + a.slippage + a.spread, abs=0.01)
    assert a.turnover == pytest.approx(65 * (100.30 + 2 * 119.70))
    assert set(a.as_dict()) >= {"brokerage", "stt", "exchange_txn", "sebi_fee", "stamp_duty", "gst", "slippage",
                                "spread", "total"}


def test_cost_model_uses_the_configured_rates() -> None:
    m = CostModel(CostsSection(brokerage_per_order=0.0, stt_sell_pct=0.0, exchange_txn_pct=0.0, sebi_fee_per_crore=0,
                               stamp_duty_buy_pct=0.0, gst_pct=0.0), slippage_ticks=0)
    a = m.attribute([Execution("s", "O", OrderSide.SELL, 65, 100.0, 100.0, 0.0, 0.0, True)])
    assert a.charges == 0 and a.execution == 0
    assert m.describe()["rates"]["brokerage_per_order"] == 0.0
    assert MarketSession().is_trading_day(date(2026, 11, 6))
