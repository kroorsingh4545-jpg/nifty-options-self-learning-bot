"""Phase 7 part 2: triple-barrier labels, forward regime labels and training
sets (SYNTHETIC data only)."""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time

import numpy as np
import pytest

from broker.charges import ChargeCalculator
from core.clock import IST
from core.enums import OptionType
from ml.dataset import (
    CandidateEvent,
    OptionHistory,
    aggregate,
    build_training_set,
    grid_events,
    strategy_events,
)
from ml.feature_engineering import Bars, PastOutcome, RegimeThresholds, outcome_features
from ml.labeling import BarrierHit, BarrierSpec, forward_regime_label, triple_barrier
from runtime.contracts import OptionUniverse
from tests.ml_support import bars_from_prices, config, synthetic_dataset

D = date(2026, 11, 4)
OPT = "NSE_FO|TESTOPT"
SPEC = BarrierSpec("PREMIUM_PERCENT", 20, "R_MULTIPLE", 2.0, max_hold_minutes=30)  # entry 100 -> stop 80, target 140
CHARGES = ChargeCalculator()


def label(ohlc: list[tuple[float, float, float, float]], *, t0: tuple[int, int] = (10, 0),
          start: tuple[int, int] = (10, 0), spec: BarrierSpec = SPEC, slippage: float = 0.0,
          square_off: time = time(15, 10)):  # type: ignore[no-untyped-def]
    bars = Bars.from_candles(bars_from_prices(OPT, D, start, ohlc))
    return triple_barrier(bars, datetime(2026, 11, 4, *t0, tzinfo=IST), spec, lot_units=65,
                          square_off_start=square_off, charges=CHARGES, slippage=slippage)


FLAT = (100.0, 101.0, 99.0, 100.0)


def test_target_first() -> None:
    lab = label([(100, 105, 98, 104), (104, 141, 103, 139), FLAT])
    assert lab.hit is BarrierHit.TARGET and lab.target_first and not lab.loss
    assert lab.entry == 100 and lab.stop == 80 and lab.target == 140 and lab.exit == 140
    assert lab.t1 == datetime(2026, 11, 4, 10, 2, tzinfo=IST) and lab.holding_minutes == 2
    assert lab.r_multiple == pytest.approx(2.0)
    assert lab.net_pnl_per_lot == pytest.approx(40 * 65 - CHARGES.round_trip(65, 100, 140))


def test_stop_first_and_charges_make_it_a_loss() -> None:
    lab = label([(100, 102, 95, 96), (96, 97, 79, 81)])
    assert lab.hit is BarrierHit.STOP and lab.exit == 80 and lab.loss
    assert lab.r_multiple == pytest.approx(-1.0) and lab.mae_pct == pytest.approx(-21.0)


def test_both_barriers_in_one_up_bar_hits_the_stop_first() -> None:
    assert label([FLAT, (100, 150, 70, 120)]).hit is BarrierHit.STOP  # up-bar: low before high


def test_both_barriers_in_one_down_bar_hits_the_target_first() -> None:
    assert label([FLAT, (120, 150, 70, 90)]).hit is BarrierHit.TARGET  # down-bar: high before low


def test_gap_through_the_stop_exits_at_the_open() -> None:
    lab = label([FLAT, (60, 62, 55, 58)])
    assert lab.hit is BarrierHit.STOP and lab.exit == 60 and lab.r_multiple == pytest.approx(-2.0)


def test_time_barrier_and_square_off_cap() -> None:
    lab = label([FLAT] * 60)
    assert lab.hit is BarrierHit.TIME and lab.holding_minutes == 30 and lab.exit == 100
    late = label([FLAT] * 60, t0=(15, 0), start=(15, 0))
    assert late.hit is BarrierHit.TIME and late.t1 == datetime(2026, 11, 4, 15, 10, tzinfo=IST)
    assert label([FLAT] * 5, t0=(15, 10), start=(15, 10)) is None  # no time left to hold


def test_no_entry_bar_means_no_label() -> None:
    assert label([FLAT] * 5, t0=(9, 50)) is None  # first bar 10 minutes after the decision
    assert label([FLAT] * 5, t0=(10, 30)) is None


def test_slippage_is_adverse_on_both_sides() -> None:
    lab = label([FLAT] * 40, slippage=0.5)
    assert lab.entry == 100.5 and lab.exit == 99.5


def test_label_depends_only_on_the_bars_between_t0_and_t1() -> None:
    before = [(500.0, 501.0, 499.0, 500.0)] * 10
    path = [(100, 105, 98, 104), (104, 141, 103, 139)]
    after = [(1.0, 2.0, 0.5, 1.5)] * 10
    a = label(path + after)
    b = label(before + path + [(900.0, 990.0, 899.0, 950.0)] * 10, start=(9, 50))
    assert a == b


def test_spec_from_strategy_and_validation() -> None:
    cfg = config()
    s = BarrierSpec.from_strategy(cfg.strategies.strategies["orb_options"])
    assert (s.stop_mode, s.stop_value, s.target_mode, s.target_value) == ("PREMIUM_PERCENT", 25, "R_MULTIPLE", 2.0)
    assert BarrierSpec("POINTS", 10, "POINTS", 15).levels(100) == (90, 115)
    assert BarrierSpec("PREMIUM_PERCENT", 10, "PREMIUM_PERCENT", 30).levels(100) == pytest.approx((90, 130))
    with pytest.raises(ValueError):
        BarrierSpec("PREMIUM_PERCENT", 100, "R_MULTIPLE", 2)
    with pytest.raises(ValueError):
        BarrierSpec("PREMIUM_PERCENT", 20, "R_MULTIPLE", 2, max_hold_minutes=0)


def test_forward_regime_label() -> None:
    up = [(25_000 + 8 * i, 25_000 + 8 * i + 4, 25_000 + 8 * i - 2, 25_000 + 8 * i + 3) for i in range(60)]
    idx = Bars.from_candles(bars_from_prices("IDX", D, (10, 0), up))
    th = RegimeThresholds()
    lab = forward_regime_label(idx, datetime(2026, 11, 4, 10, 10, tzinfo=IST), horizon_minutes=30, thresholds=th)
    assert lab is not None and lab.trend == 2
    assert lab.t1 == datetime(2026, 11, 4, 10, 40, tzinfo=IST)
    assert forward_regime_label(idx, datetime(2026, 11, 4, 10, 50, tzinfo=IST), horizon_minutes=30,
                                thresholds=th) is None  # data runs out


def test_aggregate_aligns_to_the_session_open_and_skips_incomplete_buckets() -> None:
    one = bars_from_prices("IDX", D, (9, 15), [FLAT] * 12)
    five = aggregate(one, 5, time(9, 15), time(15, 30))
    assert [c.start.time() for c in five] == [time(9, 15), time(9, 20)]  # 09:25 bucket has 2 of 5 minutes
    assert five[0].end == datetime(2026, 11, 4, 9, 20, tzinfo=IST) and five[0].volume == 5000


def test_option_selection_matches_the_runtime_universe() -> None:
    ds = synthetic_dataset()
    hist = OptionHistory(ds)
    d = ds.days()[0]
    expiry = hist.expiry_for(d)
    assert expiry is not None
    contracts = [i for i in ds.instruments if i.option_type is not None and i.expiry == expiry]
    uni = OptionUniverse(contracts, expiry=expiry, band=3)
    rules = config().strategies.strategies["orb_options"].strike_selection
    for spot in (24_910.0, 25_000.0, 25_024.9, 25_025.0, 25_133.0):
        for opt in OptionType:
            for mode, off in (("ATM", 0), ("ITM", 1), ("OTM", 2)):
                r = rules.model_copy(update={"mode": mode, "offset_steps": off})
                mine = hist.select(spot, opt, r, expiry)
                theirs = uni.select(spot, opt, mode, off)
                assert (mine and mine.instrument_key) == (theirs and theirs.instrument_key)


def test_strategy_events_replay_the_real_strategies() -> None:
    cfg, ds = config(), synthetic_dataset()
    for sid in ("orb_options", "vwap_ema"):
        evs = strategy_events(cfg, ds, sid)
        scfg = cfg.strategies.strategies[sid]
        assert evs, f"{sid} produced no signals on the SYNTHETIC data"
        per_dir = Counter((e.as_of.date(), e.option_type) for e in evs)
        cap = scfg.params["max_entries_per_direction_per_day"]
        assert max(per_dir.values()) <= cap
        for e in evs:
            assert e.as_of.minute % scfg.timeframe_minutes == 0  # a candle end on the strategy's timeframe
            assert scfg.entry_window.start <= e.as_of.time() <= scfg.entry_window.end


@pytest.fixture(scope="module")
def grid_set():  # type: ignore[no-untyped-def]
    cfg, ds = config(), synthetic_dataset()
    return build_training_set(cfg, ds, grid_events(ds.days(), strategy_id="orb_options", every_minutes=10),
                              strategy_id="orb_options")


def test_training_set_is_labelled_synthetic_and_aligned(grid_set) -> None:  # type: ignore[no-untyped-def]
    ts = grid_set
    assert ts.is_synthetic and ts.data_source == "SYNTHETIC"
    assert len(ts) > 200 and list(ts.labels["event_id"]) == list(ts.features.df["event_id"])
    assert (ts.labels["t1"] >= ts.labels["t0"]).all()
    assert (ts.labels["t0"] == ts.features.df["as_of"]).all()
    assert set(ts.labels["hit"]) <= {"TARGET", "STOP", "TIME"}
    assert ts.labels["t1"].dt.time.max() <= time(15, 10)  # never held into the square-off window
    s = ts.summary()
    assert s["data_source"] == "SYNTHETIC" and s["events"] == len(ts)
    start, end = ts.window()
    assert start < end


def test_recent_outcome_features_only_use_labels_known_by_as_of(grid_set) -> None:  # type: ignore[no-untyped-def]
    ts = grid_set
    lab, df = ts.labels, ts.features.df
    past = [PastOutcome(t1.to_pydatetime(), pnl, r) for t1, pnl, r in
            zip(lab["t1"], lab["net_pnl_per_lot"], lab["r_multiple"], strict=True)]
    still_open = 0
    for i in range(0, len(df), 7):
        as_of = df.loc[i, "as_of"].to_pydatetime()
        earlier = past[:i]  # events decided before this one ...
        known = [p for p in earlier if p.closed_at <= as_of]  # ... whose labels were known by as_of
        still_open += len(earlier) - len(known)
        want = outcome_features(known, as_of)
        for k, v in want.items():
            got = df.loc[i, k]
            assert (math.isnan(v) and math.isnan(got)) or got == pytest.approx(v), (i, k)
    assert still_open > 0  # overlapping labels existed, so the t1 <= as_of rule was exercised


def test_training_set_refuses_foreign_or_undirected_events() -> None:
    cfg, ds = config(), synthetic_dataset()
    t = datetime.combine(ds.days()[0], time(10, 0), tzinfo=IST)
    with pytest.raises(ValueError, match="belongs to"):
        build_training_set(cfg, ds, [CandidateEvent("e1", "vwap_ema", t, OptionType.CE)], strategy_id="orb_options")
    with pytest.raises(ValueError, match="direction"):
        build_training_set(cfg, ds, [CandidateEvent("e1", "orb_options", t, None)], strategy_id="orb_options")


def test_premium_bounds_drop_events(grid_set) -> None:  # type: ignore[no-untyped-def]
    ss = config().strategies.strategies["orb_options"].strike_selection
    prem = grid_set.features.df["option_premium"]
    assert ((prem >= ss.min_premium) & (prem <= ss.max_premium)).all()
    assert np.isfinite(prem).all()
