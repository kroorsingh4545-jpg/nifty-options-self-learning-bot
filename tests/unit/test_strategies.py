"""ORB and VWAP+EMA strategies on SYNTHETIC candles; indicators."""

from __future__ import annotations

from datetime import date

import pytest

from config.loader import load_config
from core.enums import OptionType
from core.models import Candle
from strategies.base_strategy import StrategyParamsError
from strategies.indicators import EMA, RollingMean, SessionVWAP
from strategies.orb_options import OrbOptionsStrategy
from strategies.vwap_ema import VwapEmaStrategy
from tests.synthetic import FUT, INDEX, at, candle, five_min, opening_range_candles

CFG = load_config(use_default_env_file=False)
ORB_CFG = CFG.strategies.strategies["orb_options"]
VWAP_CFG = CFG.strategies.strategies["vwap_ema"]


def orb() -> OrbOptionsStrategy:
    s = OrbOptionsStrategy("orb_options", ORB_CFG, signal_instrument_key=INDEX)
    s.reset_day(date(2026, 11, 3))
    return s


def feed(s: OrbOptionsStrategy | VwapEmaStrategy, candles: list[Candle]) -> list:  # type: ignore[type-arg]
    out = []
    for c in candles:
        if s.wants(c):
            out.extend(s.on_candle(c))
    return out


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #


def test_ema_seed_and_update() -> None:
    e = EMA(3)
    for x in (1, 2, 3):
        e.update(x)
    assert e.ready and e.value == pytest.approx(2.0)  # SMA seed
    e.update(6)
    assert e.value == pytest.approx(2.0 + 0.5 * (6 - 2.0))
    assert e.slope == pytest.approx(2.0)


def test_vwap_resets_daily_and_ignores_zero_volume() -> None:
    v = SessionVWAP()
    v.update(date(2026, 11, 3), 102, 98, 100, 100)
    v.update(date(2026, 11, 3), 112, 108, 110, 300)
    assert v.value == pytest.approx((100 * 100 + 110 * 300) / 400)
    v.update(date(2026, 11, 3), 500, 500, 500, 0)
    assert v.value == pytest.approx((100 * 100 + 110 * 300) / 400)
    v.update(date(2026, 11, 4), 52, 48, 50, 10)
    assert v.value == pytest.approx(50)


def test_rolling_mean() -> None:
    r = RollingMean(3)
    for x in (1, 2, 3, 10):
        r.update(x)
    assert r.full and r.mean == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# ORB
# --------------------------------------------------------------------------- #


def test_orb_builds_range_from_0915_to_0930() -> None:
    s = orb()
    assert feed(s, opening_range_candles(high=24520, low=24480)) == []
    snap = s.snapshot()["opening_range"]
    assert snap == {"high": 24520, "low": 24480, "ready": True, "invalid": None, "minutes": 15}


def test_orb_ce_on_confirmed_close_above_high_plus_buffer() -> None:
    s = orb()
    feed(s, opening_range_candles())
    sigs = feed(s, five_min(INDEX, at(9, 30), [24510, 24524, 24531]))  # buffer 5 -> needs > 24525
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.option_type is OptionType.CE and sig.strategy_id == "orb_options"
    assert sig.candle_end == at(9, 45) and sig.underlying_price == 24531
    assert sig.features["or_high"] == 24520 and "breakout" in sig.reason.lower()


def test_orb_pe_on_close_below_low() -> None:
    s = orb()
    feed(s, opening_range_candles())
    sigs = feed(s, five_min(INDEX, at(9, 30), [24470]))
    assert [x.option_type for x in sigs] == [OptionType.PE]


def test_orb_wick_without_close_does_not_trigger() -> None:
    s = orb()
    feed(s, opening_range_candles())
    c = candle(INDEX, 5, at(9, 30), 24510, 24600, 24505, 24519)  # spikes above, closes inside
    assert feed(s, [c]) == []


def test_orb_no_signal_before_range_complete_or_outside_window() -> None:
    s = orb()
    early = candle(INDEX, 5, at(9, 25), 24500, 24600, 24490, 24590)
    assert feed(s, [early]) == []  # range not ready
    feed(s, opening_range_candles())
    late = five_min(INDEX, at(10, 55), [24600])  # ends 11:00 -> outside window
    assert feed(s, late) == []


def test_orb_does_not_repeat_while_outside_and_rearms_after_reentry() -> None:
    s = orb()
    feed(s, opening_range_candles())
    first = feed(s, five_min(INDEX, at(9, 30), [24540, 24560, 24580]))
    assert len(first) == 1  # transition only
    s.on_entry_result(first[0], approved=False)  # risk rejected it (e.g. spread)
    again = feed(s, five_min(INDEX, at(9, 45), [24510, 24545]))  # back inside, then out again
    assert len(again) == 1


def test_orb_one_entry_per_direction_per_day() -> None:
    s = orb()
    feed(s, opening_range_candles())
    (sig,) = feed(s, five_min(INDEX, at(9, 30), [24540]))
    s.on_entry_result(sig, approved=True)
    assert feed(s, five_min(INDEX, at(9, 35), [24510, 24550])) == []  # CE quota used
    pe = feed(s, five_min(INDEX, at(9, 45), [24470]))
    assert [x.option_type for x in pe] == [OptionType.PE]  # other direction still allowed


@pytest.mark.parametrize(("hi", "lo", "why"), [(24485, 24480, "points"), (25000, 24480, "%")])
def test_orb_invalid_range_skips_day(hi: float, lo: float, why: str) -> None:
    s = orb()
    feed(s, opening_range_candles(high=hi, low=lo))
    assert why in (s.range_invalid or "")
    assert feed(s, five_min(INDEX, at(9, 30), [26000])) == []


def test_orb_resets_next_day() -> None:
    s = orb()
    feed(s, opening_range_candles())
    (sig,) = feed(s, five_min(INDEX, at(9, 30), [24540]))
    s.on_entry_result(sig, True)
    nxt = (2026, 11, 4)
    feed(s, opening_range_candles(high=24620, low=24580, day=nxt))
    assert s.entries[OptionType.CE] == 0 and s.or_high == 24620
    assert len(feed(s, five_min(INDEX, at(9, 30, day=nxt), [24640]))) == 1


def test_orb_ignores_other_instruments_and_incomplete_candles() -> None:
    s = orb()
    assert not s.wants(candle(FUT, 5, at(9, 30), 1, 2, 1, 2))
    c = candle(INDEX, 5, at(9, 30), 1, 2, 1, 2).model_copy(update={"complete": False})
    assert not s.wants(c)
    assert not s.wants(candle(INDEX, 15, at(9, 15), 1, 2, 1, 2))


def test_bad_params_rejected() -> None:
    bad = ORB_CFG.model_copy(update={"params": {**ORB_CFG.params, "unknown": 1}})
    with pytest.raises(StrategyParamsError):
        OrbOptionsStrategy("orb_options", bad, signal_instrument_key=INDEX)


# --------------------------------------------------------------------------- #
# VWAP + EMA
# --------------------------------------------------------------------------- #


def vwap() -> VwapEmaStrategy:
    s = VwapEmaStrategy("vwap_ema", VWAP_CFG, signal_instrument_key=FUT)
    s.reset_day(date(2026, 11, 3))
    return s


def flat_warmup(s: VwapEmaStrategy, price: float = 24500, n: int = 20) -> None:
    """Prior-session candles to seed the EMA (point-in-time: strictly earlier)."""
    s.warm_up(five_min(FUT, at(14, 0, day=(2026, 11, 2)), [price] * n))


def test_vwap_ce_in_uptrend_with_volume() -> None:
    s = vwap()
    flat_warmup(s)
    closes = [24496, 24497, 24498, 24499, 24500, 24510, 24525, 24545]
    vols = [10000, 10000, 10000, 10000, 10000, 15000, 18000, 20000]
    sigs = feed(s, five_min(FUT, at(9, 15), closes, vols))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.option_type is OptionType.CE
    assert sig.features["close"] > sig.features["vwap"] and sig.features["close"] > sig.features["ema"]
    assert sig.features["volume_ratio"] >= 1.2 and sig.features["ema_slope"] > 0
    assert sig.candle_end >= at(9, 45)  # inside 09:45–14:30 entry window


def test_vwap_pe_in_downtrend() -> None:
    s = vwap()
    flat_warmup(s)
    closes = [24504, 24503, 24502, 24501, 24500, 24490, 24475, 24455]
    vols = [10000] * 5 + [15000, 18000, 20000]
    sigs = feed(s, five_min(FUT, at(9, 15), closes, vols))
    assert [x.option_type for x in sigs] == [OptionType.PE]


def test_vwap_needs_warmup() -> None:
    s = vwap()  # no warm-up: EMA20 not ready after 8 candles
    closes = [24496, 24497, 24498, 24499, 24500, 24510, 24525, 24545]
    assert feed(s, five_min(FUT, at(9, 15), closes, [10000] * 5 + [15000, 18000, 20000])) == []
    assert s.last_reason == "warming up"


def test_vwap_low_volume_filter() -> None:
    s = vwap()
    flat_warmup(s)
    closes = [24496, 24497, 24498, 24499, 24500, 24510, 24525, 24545]
    assert feed(s, five_min(FUT, at(9, 15), closes, [10000] * 8)) == []  # breakout without volume
    assert "low volume" in (s.last_reason or "")


def test_vwap_chop_filter() -> None:
    s = vwap()
    flat_warmup(s)
    zigzag = [24500, 24510, 24490, 24512, 24488, 24514, 24486, 24516, 24530, 24545]
    vols = [10000] * 8 + [20000, 25000]
    assert feed(s, five_min(FUT, at(9, 15), zigzag, vols)) == []
    assert "choppy" in (s.last_reason or "")


def test_vwap_no_repeat_signal_during_trend() -> None:
    s = vwap()
    flat_warmup(s)
    closes = [24496, 24497, 24498, 24499, 24500, 24510, 24525, 24545, 24565, 24585]
    vols = [10000] * 5 + [15000, 18000, 20000, 25000, 30000]
    assert len(feed(s, five_min(FUT, at(9, 15), closes, vols))) == 1


def test_vwap_respects_entry_window() -> None:
    s = vwap()
    flat_warmup(s)
    closes = [24496, 24497, 24498, 24499, 24500, 24510, 24525, 24545]
    vols = [10000] * 5 + [15000, 18000, 20000]
    assert feed(s, five_min(FUT, at(14, 0), closes, vols)) == []  # ends after 14:30


def test_vwap_rejects_unsupported_timeframe() -> None:
    bad = VWAP_CFG.model_copy(update={"timeframe_minutes": 15})
    with pytest.raises(ValueError, match="3- or 5-minute"):
        VwapEmaStrategy("vwap_ema", bad, signal_instrument_key=FUT)


def test_strategies_are_isolated_objects() -> None:
    a, b = orb(), orb()
    feed(a, opening_range_candles())
    assert b.or_high is None  # no shared state
    assert a.snapshot()["strategy_id"] == "orb_options"


def test_signal_uses_injected_clock_and_is_point_in_time() -> None:
    from tests.fakes import FakeWallClock

    wall = FakeWallClock(at(9, 35, 1))
    s = OrbOptionsStrategy("orb_options", ORB_CFG, signal_instrument_key=INDEX, now_fn=wall)
    feed(s, opening_range_candles())
    (sig,) = feed(s, five_min(INDEX, at(9, 30), [24540]))
    assert sig.candle_end == at(9, 35)
    assert sig.generated_at == at(9, 35, 1)  # simulated time, not the real wall clock
    assert sig.generated_at >= sig.candle_end  # generated only after the data it used
