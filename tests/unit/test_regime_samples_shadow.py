"""D4: historical samples (point in time, forward windows) and regime labels at
shadow signals (SYNTHETIC bars)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from core.clock import IST
from research.regime.pulse import fit
from research.regime.samples import DailyHistory, SampleSettings, build_samples
from research.regime.shadow import RegimeContext, regime_row
from research.store import ResearchStore

DAYS = [date(2026, 3, 2) + timedelta(days=k) for k in range(30) if (date(2026, 3, 2) + timedelta(days=k)).weekday() < 5]


def session(day: date, seed: int, *, drop: set[int] | None = None) -> dict[time, tuple[float, float, float, float]]:
    rng = np.random.default_rng(seed)
    price, bars = 22000.0, {}
    for m in range(375):
        if drop and m in drop:
            continue
        o = price
        price = o * math.exp(rng.normal(1e-5, 4e-4))
        t = (datetime.combine(day, time(9, 15)) + timedelta(minutes=m)).time()
        bars[t] = (o, max(o, price) + 2, min(o, price) - 2, price)
    return bars


def daily() -> DailyHistory:
    prior = [date(2025, 1, 1) + timedelta(days=k) for k in range(400)]
    rng = np.random.default_rng(1)
    c = list(22000 * np.exp(np.cumsum(rng.normal(0, 0.009, len(prior)))))
    v = list(14 * np.exp(np.cumsum(rng.normal(0, 0.03, len(prior))) * 0.2))
    return DailyHistory(prior + DAYS, c + [22000.0] * len(DAYS), c + [22000.0] * len(DAYS), prior, v)


SETTINGS = SampleSettings(checkpoints=(time(10, 0), time(12, 0)), horizon=30)


def samples(bars: dict[date, dict[time, tuple[float, float, float, float]]]) -> list:  # type: ignore[type-arg]
    vix = {d: {(datetime.combine(d, t) + timedelta(minutes=1)).time(): 14.0 for t in bars[d]} for d in bars}
    return build_samples(bars, vix, daily(), {}, set(), SETTINGS, tz=IST)


def test_checkpoint_samples_and_their_outcomes() -> None:
    bars = {DAYS[0]: session(DAYS[0], 1)}
    cps = [s for s in samples(bars) if s.kind == "CHECKPOINT"]
    assert [s.t0.time() for s in cps] == [time(10, 0), time(12, 0)]
    s = cps[0]
    closes = {(datetime.combine(DAYS[0], t) + timedelta(minutes=1)).time(): b[3] for t, b in bars[DAYS[0]].items()}
    fwd = math.log(closes[time(10, 30)] / closes[time(10, 0)])
    assert s.outcomes["fwd_ret"] == pytest.approx(fwd)
    assert s.outcomes["cont"] == pytest.approx(fwd * s.direction / (s.features["sd"] * math.sqrt(30)))
    assert s.t1 - s.t0 == timedelta(minutes=30) and s.regime["vol"] is not None


def test_features_never_see_the_forward_window() -> None:
    a = session(DAYS[0], 1)
    b = dict(a)
    for t in list(b):
        if t >= time(10, 0):  # change every bar from 10:00 on
            o, h, lo, c = b[t]
            b[t] = (o * 1.01, h * 1.01, lo * 1.01, c * 1.01)
    sa = [s for s in samples({DAYS[0]: a}) if s.t0.time() == time(10, 0)][0]
    sb = [s for s in samples({DAYS[0]: b}) if s.t0.time() == time(10, 0)][0]
    assert sa.features == sb.features and sa.outcomes != sb.outcomes


def test_a_missing_minute_in_the_forward_window_skips_the_sample() -> None:
    bars = {DAYS[0]: session(DAYS[0], 1, drop={60})}  # 10:15 missing
    assert time(10, 0) not in [s.t0.time() for s in samples(bars) if s.kind == "CHECKPOINT"]


def test_orb_samples_carry_the_signal_direction() -> None:
    found = []
    for k, d in enumerate(DAYS):
        found += [s for s in samples({d: session(d, 100 + k)}) if s.kind == "ORB"]
    assert found and all(s.direction == (1 if s.info["option_type"] == "CE" else -1) for s in found)


def ctx(bars: dict[date, dict[time, tuple[float, float, float, float]]], *, with_cutoffs: bool = True) -> RegimeContext:
    hist = [s for d in DAYS[:15] for s in samples({d: session(d, DAYS.index(d))}) if s.kind == "CHECKPOINT"]
    cut = fit([s.features for s in hist], min_train=10) if with_cutoffs else None
    return RegimeContext(SETTINGS, "d4.test", daily(), set(), lambda d: bars.get(d, {}), lambda t: 15.0,
                         lambda d: cut, ["vol_change_iv_bias"])


def outcome(at: datetime, opt: str = "CE") -> dict[str, object]:
    return {"signal_id": "SIG-1", "sim_version": "d2.1", "generated_at": at.isoformat(), "option_type": opt}


def test_a_shadow_signal_gets_pulse_and_regime_from_bars_before_it() -> None:
    d = DAYS[20]
    bars = {d: session(d, 7)}
    t = datetime.combine(d, time(11, 0, 0, 300_000), tzinfo=IST)
    row = regime_row(outcome(t), ctx(bars))
    assert row is not None and row["pulse_label"] in ("STRONG_TREND", "DEVELOPING", "WEAK", "RANGE_CHOP")
    assert row["regime_named"] and json.loads(str(row["kept_json"])) == ["vol_change_iv_bias"]
    later = {d: {k: v if k < time(11, 0) else (v[0] * 2, v[1] * 2, v[2] * 2, v[3] * 2) for k, v in bars[d].items()}}
    again = regime_row(outcome(t), ctx(later))
    assert again is not None and (again["er"], again["adx"], again["pulse_label"]) == (
        row["er"], row["adx"], row["pulse_label"])  # bars after the signal are never used


def test_without_cutoffs_or_bars_the_pulse_is_uncertain() -> None:
    d = DAYS[20]
    t = datetime.combine(d, time(11, 0), tzinfo=IST)
    assert regime_row(outcome(t), ctx({d: session(d, 7)}, with_cutoffs=False))["pulse_label"] == "UNCERTAIN"  # type: ignore[index]
    assert regime_row(outcome(t), ctx({}))["pulse_label"] == "UNCERTAIN"  # type: ignore[index]


def test_regime_rows_are_stored(tmp_path: Path) -> None:
    d = DAYS[20]
    row = regime_row(outcome(datetime.combine(d, time(11, 0), tzinfo=IST)), ctx({d: session(d, 7)}))
    assert row is not None
    store = ResearchStore.open(tmp_path / "research.db")
    assert store.upsert_regime([row]) == 1 and store.upsert_regime([row]) == 1
    (got,) = store.regime_rows("d2.1", "d4.test")
    assert got["pulse_label"] == row["pulse_label"]
    store.close()
