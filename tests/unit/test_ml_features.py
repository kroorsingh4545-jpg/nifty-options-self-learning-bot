"""Phase 7 part 1: point-in-time features and the feature store (SYNTHETIC data only)."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core.clock import IST
from core.enums import OptionType, TradingMode
from core.models import Candle
from data.storage.repositories import Repositories
from ml.feature_engineering import (
    FEATURE_NAMES,
    Bars,
    FeatureEngine,
    LookaheadError,
    OptionContext,
    PastOutcome,
    SessionTimes,
    compute_features,
    feature_schema,
    feature_vector,
    trailing_regime,
)
from ml.feature_store import FeatureFrame, FeatureStore, FeatureStoreError, as_of_join
from options.pricing import YEAR_SECONDS, black_scholes, implied_volatility

INDEX = "NSE_INDEX|Nifty 50"
FUT = "NSE_FO|SYNTHFUT"
TIMES = SessionTimes()
D1, D2 = date(2026, 11, 3), date(2026, 11, 4)


def synthetic_day(day: date, key: str = INDEX, *, seed: int = 1, start: float = 25_000.0, drift: float = 0.0,
                  vol: float = 0.0004, volume: int = 0, minutes: int = 375) -> list[Candle]:
    """SYNTHETIC 1-minute random-walk candles (tests only; not real prices)."""
    rng = random.Random(seed)  # noqa: S311
    out, level = [], start
    t0 = datetime.combine(day, TIMES.open, tzinfo=IST)
    for i in range(minutes):
        o = level
        level *= 1 + drift + rng.gauss(0, vol)
        c = level
        hi, lo = max(o, c) * (1 + abs(rng.gauss(0, vol / 3))), min(o, c) * (1 - abs(rng.gauss(0, vol / 3)))
        s = t0 + timedelta(minutes=i)
        out.append(Candle(instrument_key=key, timeframe_minutes=1, start=s, end=s + timedelta(minutes=1),
                          open=round(o, 2), high=round(hi, 2), low=round(lo, 2), close=round(c, 2),
                          volume=volume + rng.randint(0, 500) if volume else 0))
    return out


def two_days() -> list[Candle]:
    return synthetic_day(D1, seed=1) + synthetic_day(D2, seed=2, start=25_100.0)


def _same(a: dict[str, float], b: dict[str, float]) -> bool:
    return all((math.isnan(a[k]) and math.isnan(b[k])) or a[k] == b[k] for k in FEATURE_NAMES)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_schema_is_versioned_unique_and_stable() -> None:
    s = feature_schema()
    assert s["version"] == "1.0.0"
    names = [f["name"] for f in s["features"]]
    assert names == list(FEATURE_NAMES) and len(set(names)) == len(names)
    assert s["hash"] == feature_schema()["hash"]
    required = {"ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_30m", "vwap_dist_pct", "ema20_dist_pct",
                "ema20_slope_pct", "atr14_pct", "rv_15m", "rv_30m", "or_position", "minutes_since_open",
                "minutes_to_expiry", "option_premium", "option_iv", "option_oi_log", "option_volume_log",
                "option_spread_pct", "recent_win_rate_5", "consecutive_losses", "regime_trend", "regime_vol"}
    assert required <= set(names)


def test_feature_vector_refuses_a_different_schema() -> None:
    f = compute_features(Bars.from_candles(two_days()), datetime(2026, 11, 4, 11, 0, tzinfo=IST), times=TIMES)
    assert feature_vector(f).shape == (len(FEATURE_NAMES),)
    with pytest.raises(ValueError, match="missing"):
        feature_vector({k: v for k, v in f.items() if k != "ret_1m"})
    with pytest.raises(ValueError, match="unexpected"):
        feature_vector({**f, "future_return": 1.0})


# --------------------------------------------------------------------------- #
# Point-in-time / no lookahead
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hhmm", [(9, 16), (9, 29), (9, 30), (9, 47), (11, 0), (14, 44), (15, 29)])
def test_changing_the_future_never_changes_features(hhmm: tuple[int, int]) -> None:
    """The leakage test: rewrite every bar that ends after as-of (index,
    futures, outcomes) and the features at as-of must be bit-identical."""
    as_of = datetime(2026, 11, 4, *hhmm, tzinfo=IST)
    idx = two_days()
    fut = synthetic_day(D1, FUT, seed=3, volume=4000) + synthetic_day(D2, FUT, seed=4, volume=4000)
    past = [PastOutcome(as_of - timedelta(minutes=30), -500.0, -1.0), PastOutcome(as_of, 800.0, 1.6)]
    opt = OptionContext(OptionType.CE, 25_100.0, D2 + timedelta(days=6), as_of, premium=120.0, day_volume=1e5,
                        oi=2e6, bid=119.9, ask=120.1)

    def run(index: list[Candle], futs: list[Candle], outcomes: list[PastOutcome]) -> dict[str, float]:
        return compute_features(Bars.from_candles(index), as_of, times=TIMES, direction=OptionType.CE,
                                futures_bars=Bars.from_candles(futs), option=opt, outcomes=outcomes)

    base = run(idx, fut, past)

    def scramble(cs: list[Candle]) -> list[Candle]:
        rng = random.Random(99)  # noqa: S311
        out = []
        for c in cs:
            if c.end <= as_of:
                out.append(c)
            else:
                k = 1 + rng.uniform(-0.05, 0.05)
                o, cl = c.open * k, c.close * (2 - k)
                out.append(c.model_copy(update={"open": o, "close": cl, "high": max(o, cl) + 50,
                                                "low": min(o, cl) - 50, "volume": c.volume * 7}))
        return out

    future_outcomes = [*past, PastOutcome(as_of + timedelta(minutes=1), -9999.0, -5.0)]
    assert _same(base, run(scramble(idx), scramble(fut), future_outcomes))
    # And cutting the data off at as-of gives the same answer as passing the whole day.
    assert _same(base, run([c for c in idx if c.end <= as_of], [c for c in fut if c.end <= as_of], past))


def test_option_context_after_as_of_is_refused() -> None:
    as_of = datetime(2026, 11, 4, 10, 0, tzinfo=IST)
    late = OptionContext(OptionType.PE, 25_000.0, D2, as_of + timedelta(seconds=1), premium=80.0)
    with pytest.raises(LookaheadError):
        compute_features(Bars.from_candles(two_days()), as_of, times=TIMES, option=late)


def test_recent_outcomes_only_count_trades_closed_by_as_of() -> None:
    as_of = datetime(2026, 11, 4, 12, 0, tzinfo=IST)
    outs = [PastOutcome(as_of - timedelta(hours=2), 100.0, 0.5), PastOutcome(as_of - timedelta(hours=1), -50.0, -1.0),
            PastOutcome(as_of - timedelta(minutes=5), -60.0, -1.0), PastOutcome(as_of + timedelta(minutes=5), 900, 3.0)]
    f = compute_features(Bars.from_candles(two_days()), as_of, times=TIMES, outcomes=outs)
    assert f["recent_win_rate_5"] == pytest.approx(1 / 3)
    assert f["consecutive_losses"] == 2.0
    assert f["recent_avg_r_5"] == pytest.approx((0.5 - 1 - 1) / 3)


def test_intraday_features_never_mix_sessions() -> None:
    """At 09:16 on day 2 only one bar of day 2 exists: multi-bar returns are
    missing rather than spanning the overnight gap; gap_pct uses the prior close."""
    idx = two_days()
    f = compute_features(Bars.from_candles(idx), datetime(2026, 11, 4, 9, 16, tzinfo=IST), times=TIMES)
    assert math.isnan(f["ret_1m"]) and math.isnan(f["ret_30m"]) and math.isnan(f["ema20_dist_pct"])
    prev_close = [c for c in idx if c.start.date() == D1][-1].close
    first = [c for c in idx if c.start.date() == D2][0]
    assert f["gap_pct"] == pytest.approx((first.open / prev_close - 1) * 100)
    assert math.isnan(f["or_position"])  # opening range not complete yet


def test_feature_values() -> None:
    idx = two_days()
    as_of = datetime(2026, 11, 4, 10, 30, tzinfo=IST)
    f = compute_features(Bars.from_candles(idx), as_of, times=TIMES, direction=OptionType.PE)
    day = [c for c in idx if c.start.date() == D2 and c.end <= as_of]
    closes = np.array([c.close for c in day])
    assert f["direction"] == -1.0
    assert f["ret_1m"] == pytest.approx(math.log(closes[-1] / closes[-2]) * 100)
    assert f["ret_15m"] == pytest.approx(math.log(closes[-1] / closes[-16]) * 100)
    assert f["minutes_since_open"] == 75.0
    assert f["minutes_to_square_off"] == pytest.approx((15 * 60 + 10) - (10 * 60 + 30))
    rets = np.diff(np.log(closes[-31:]))
    assert f["rv_30m"] == pytest.approx(float(np.std(rets, ddof=1)) * math.sqrt(375 * 252))
    orb = [c for c in day if c.end.time() <= TIMES.opening_range_end]
    hi, lo = max(c.high for c in orb), min(c.low for c in orb)
    assert f["or_position"] == pytest.approx((closes[-1] - lo) / (hi - lo))
    assert math.isnan(f["vwap_dist_pct"])  # no futures (index has no volume)
    assert math.isnan(f["option_premium"]) and math.isnan(f["minutes_to_expiry"])


def test_vwap_distance_from_futures_volume() -> None:
    as_of = datetime(2026, 11, 4, 11, 0, tzinfo=IST)
    fut = synthetic_day(D2, FUT, seed=5, volume=3000)
    seen = [c for c in fut if c.end <= as_of]
    vwap = sum((c.high + c.low + c.close) / 3 * c.volume for c in seen) / sum(c.volume for c in seen)
    f = compute_features(Bars.from_candles(two_days()), as_of, times=TIMES, futures_bars=Bars.from_candles(fut))
    assert f["vwap_dist_pct"] == pytest.approx((seen[-1].close - vwap) / vwap * 100)


def test_option_features_and_implied_volatility() -> None:
    as_of = datetime(2026, 11, 4, 11, 0, tzinfo=IST)
    expiry = date(2026, 11, 10)
    spot_bars = Bars.from_candles(two_days())
    spot = float(spot_bars.upto(as_of).close[-1])
    years = (datetime.combine(expiry, TIMES.close, tzinfo=IST) - as_of).total_seconds() / YEAR_SECONDS
    premium = black_scholes(spot, 25_000.0, years, 0.14, TIMES.rate, False)
    opt = OptionContext(OptionType.PE, 25_000.0, expiry, as_of, premium=premium, day_volume=0, oi=None, bid=None,
                        ask=None)
    f = compute_features(spot_bars, as_of, times=TIMES, option=opt)
    assert f["option_iv"] == pytest.approx(0.14, abs=1e-4)
    assert f["option_moneyness_pct"] == pytest.approx(-(spot - 25_000) / spot * 100)
    assert f["minutes_to_expiry"] == pytest.approx(years * YEAR_SECONDS / 60)
    assert f["is_expiry_day"] == 0.0 and f["option_volume_log"] == 0.0
    assert math.isnan(f["option_spread_pct"]) and math.isnan(f["option_oi_log"])
    # feed IV wins over the solved one; expiry-day flag
    opt2 = OptionContext(OptionType.CE, 25_000.0, D2, as_of, premium=50.0, iv=0.2, bid=49.0, ask=51.0)
    f2 = compute_features(spot_bars, as_of, times=TIMES, option=opt2)
    assert f2["option_iv"] == 0.2 and f2["is_expiry_day"] == 1.0 and f2["option_spread_pct"] == pytest.approx(4.0)


def test_implied_volatility_bounds() -> None:
    assert implied_volatility(0.0, 25_000, 25_000, 0.01, 0.065, True) is None
    assert implied_volatility(10.0, 25_000, 24_000, 0.01, 0.065, True) is None  # below intrinsic
    assert implied_volatility(100.0, 25_000, 25_000, 0.0, 0.065, True) is None  # expired
    p = black_scholes(25_000, 25_200, 5 / 365, 0.12, 0.065, True)
    assert implied_volatility(p, 25_000, 25_200, 5 / 365, 0.065, True) == pytest.approx(0.12, abs=1e-5)


def test_trailing_regime_rules() -> None:
    up = np.array([25_000 * (1.0004 ** i) for i in range(40)]) * (1 + 0.0001 * np.sin(np.arange(40)))
    trend, _ = trailing_regime(up)
    assert trend == 1.0
    flat = 25_000 + np.tile([0.0, 3.0], 20)
    assert trailing_regime(flat)[0] == 0.0
    assert all(math.isnan(x) for x in trailing_regime(up[:10]))
    calm = 25_000 * np.exp(np.cumsum(np.tile([0.0002, -0.0002], 20)))
    assert trailing_regime(calm)[1] == -1.0
    wild = 25_000 * np.exp(np.cumsum(np.tile([0.003, -0.003], 20)))
    assert trailing_regime(wild)[1] == 1.0


def test_live_engine_matches_batch_features() -> None:
    idx = two_days()
    fut = synthetic_day(D1, FUT, seed=3, volume=4000) + synthetic_day(D2, FUT, seed=4, volume=4000)
    eng = FeatureEngine(index_key=INDEX, futures_key=FUT, times=TIMES)
    as_of = datetime(2026, 11, 4, 13, 5, tzinfo=IST)
    for c in sorted(idx + fut, key=lambda c: (c.start, c.instrument_key)):
        if c.end <= as_of:
            eng.on_candle(c)
            eng.on_candle(c)  # duplicates are ignored
    eng.on_candle(Candle(instrument_key=INDEX, timeframe_minutes=5, start=as_of - timedelta(minutes=5), end=as_of,
                         open=1, high=1, low=1, close=1))  # other timeframes are ignored
    live = eng.features(as_of, direction=OptionType.CE)
    batch = compute_features(Bars.from_candles(idx), as_of, times=TIMES, direction=OptionType.CE,
                             futures_bars=Bars.from_candles(fut))
    assert _same(live, batch)
    assert eng.last_bar_end == as_of


def test_bars_must_be_increasing() -> None:
    cs = synthetic_day(D1, minutes=3)
    with pytest.raises(ValueError):
        Bars.from_candles([cs[1], cs[0]])


# --------------------------------------------------------------------------- #
# Feature store
# --------------------------------------------------------------------------- #


def _frame(source: str = "SYNTHETIC", n: int = 4) -> FeatureFrame:
    idx = Bars.from_candles(two_days())
    rows = []
    for i in range(n):
        t = datetime(2026, 11, 4, 10, 0, tzinfo=IST) + timedelta(minutes=5 * i)
        rows.append({"event_id": f"e{i}", "as_of": t, "strategy_id": "orb_options", "option_type": "CE",
                     **compute_features(idx, t, times=TIMES, direction=OptionType.CE)})
    return FeatureFrame.from_rows(rows, source, "SYNTHETIC test rows")  # type: ignore[arg-type]


def test_feature_frame_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fr = _frame()
    assert fr.is_synthetic and fr.matrix().shape == (4, len(FEATURE_NAMES))
    back = FeatureFrame.load(fr.save(tmp_path / "f"))
    assert back.data_source == "SYNTHETIC"
    np.testing.assert_array_equal(np.isnan(back.matrix()), np.isnan(fr.matrix()))
    np.testing.assert_allclose(np.nan_to_num(back.matrix()), np.nan_to_num(fr.matrix()))
    assert list(back.df["as_of"]) == list(fr.df["as_of"])
    assert 0 < fr.coverage()["ret_30m"] <= 1


def test_feature_frame_guards(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fr = _frame()
    with pytest.raises(FeatureStoreError, match="mix"):
        fr.concat(_frame("HISTORICAL"))
    with pytest.raises(FeatureStoreError, match="data_source"):
        FeatureFrame(fr.df, "LIVE")  # type: ignore[arg-type]
    with pytest.raises(FeatureStoreError, match="sorted"):
        FeatureFrame(fr.df.iloc[::-1].reset_index(drop=True), "SYNTHETIC")
    with pytest.raises(FeatureStoreError, match="duplicate"):
        FeatureFrame(pd.concat([fr.df, fr.df.iloc[-1:]], ignore_index=True), "SYNTHETIC")
    with pytest.raises(FeatureStoreError, match="columns"):
        FeatureFrame(fr.df.drop(columns=["ret_1m"]), "SYNTHETIC")
    d = fr.save(tmp_path / "f")
    meta = (d / "meta.json").read_text(encoding="utf-8").replace(fr.schema_hash, "0" * 16)
    (d / "meta.json").write_text(meta, encoding="utf-8")
    with pytest.raises(FeatureStoreError, match="does not match"):
        FeatureFrame.load(d)


def test_as_of_join_never_uses_a_later_row() -> None:
    fr = _frame(n=4)  # rows at 10:00, 10:05, 10:10, 10:15
    events = pd.DataFrame({"time": pd.to_datetime(["2026-11-04T09:59:00+05:30", "2026-11-04T10:05:00+05:30",
                                                   "2026-11-04T10:09:59+05:30"]).tz_convert(IST)})
    out = as_of_join(events, fr.df)
    assert pd.isna(out.loc[0, "event_id"])  # nothing at or before 09:59
    assert list(out["event_id"][1:]) == ["e1", "e1"]


def test_feature_store_records_signal_snapshots(paper: Repositories) -> None:
    store = FeatureStore(paper.feature_snapshots)
    feats = compute_features(Bars.from_candles(two_days()), datetime(2026, 11, 4, 10, 0, tzinfo=IST), times=TIMES)
    sid = store.record_signal(strategy_id="orb_options", instrument_key=INDEX,
                              as_of=datetime(2026, 11, 4, 10, 0, tzinfo=IST), features=feats,
                              extra={"model_version": "none"})
    row = paper.db.query_one("SELECT * FROM feature_snapshots WHERE id = ?", (sid,))
    assert row["purpose"] == "SIGNAL" and row["mode"] == TradingMode.PAPER.value
    assert row["feature_schema_version"] == "1.0.0"
