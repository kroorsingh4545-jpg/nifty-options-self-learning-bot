"""Historical samples for regime / Momentum Pulse validation (pure).

Two kinds, both from the stored NIFTY 1-minute history:

* ``CHECKPOINT``: the state at fixed times of every session (e.g. 10:00 ..
  14:30), direction = sign of the trailing displacement;
* ``ORB``: every ORB signal the strategy's rule would have given
  (``research.regime.orb``), direction = the signal's.

Features use only bars up to the sample time (point in time); the regime uses
only daily data of earlier sessions plus India VIX at the sample time. Outcomes
look ``horizon`` minutes ahead inside the same session (``t1``), and a sample
whose forward window has a missing minute is skipped. Outcomes are unit-free:

* ``cont``: forward log return in the sample's direction / (trailing 1-minute sd
  x sqrt(horizon)): > 0 = the move continued
* ``log_rv_trailing``: log(forward realised variance / trailing variance)
* ``log_rv_implied``: log(forward realised variance / VIX business-time implied
  variance of the same minutes, ``volatility.business_time``)
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from ml.feature_engineering import trailing_regime
from research.regime.indicators import adx, atr_expansion, displacement, efficiency_ratio, realised_sd
from research.regime.orb import OrbRule, orb_signals
from research.regime.regime import RegimeSettings, classify, named
from volatility.business_time import vix_clock, window_variance

Bar = tuple[float, float, float, float]  # open, high, low, close


@dataclass(frozen=True)
class SampleSettings:
    checkpoints: tuple[time, ...] = (time(10, 0), time(10, 30), time(11, 0), time(11, 30), time(12, 0),
                                     time(12, 30), time(13, 0), time(13, 30), time(14, 0), time(14, 30))
    horizon: int = 30
    lookback: int = 20  # bars for efficiency ratio, displacement and the trailing sd
    adx_period: int = 10
    atr_short: int = 10
    atr_long: int = 20
    orb: OrbRule = OrbRule()
    regime: RegimeSettings = RegimeSettings()


@dataclass
class Sample:
    kind: str  # CHECKPOINT | ORB
    day: date
    t0: datetime
    t1: datetime
    direction: int
    features: dict[str, float]
    regime: dict[str, Any]
    outcomes: dict[str, float]
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyHistory:
    """Daily closes (oldest first) for point-in-time lookups."""

    days: Sequence[date]
    nifty_open: Sequence[float]
    nifty_close: Sequence[float]
    vix_days: Sequence[date]
    vix_close: Sequence[float]

    #: the regime looks back at most this many sessions (lookback + change window + trend history)
    WINDOW = 330

    def before(self, day: date) -> tuple[list[float], list[float]]:
        """The trailing daily closes of the sessions strictly before ``day``."""
        i = bisect.bisect_left(self.days, day)
        j = bisect.bisect_left(self.vix_days, day)
        return list(self.nifty_close[max(0, i - self.WINDOW):i]), list(self.vix_close[max(0, j - self.WINDOW):j])

    def open_of(self, day: date) -> float | None:
        i = bisect.bisect_left(self.days, day)
        return float(self.nifty_open[i]) if i < len(self.days) and self.days[i] == day else None


def _minute(t: time, k: int) -> time:
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=k)).time()


def session_series(bars: Mapping[time, Bar], s: SampleSettings) -> dict[str, Any]:
    starts = sorted(bars)
    o, h, lo, c = (np.asarray([bars[t][k] for t in starts], dtype=np.float64) for k in range(4))
    disp = displacement(h, lo, c, s.lookback, s.atr_short)
    return {"starts": starts, "index": {t: i for i, t in enumerate(starts)}, "close": c,
            "er": efficiency_ratio(c, s.lookback), "adx": adx(h, lo, c, s.adx_period), "disp": disp,
            "atrx": atr_expansion(h, lo, c, s.atr_short, s.atr_long), "sd": realised_sd(c, s.lookback)}


def features_at(ser: Mapping[str, Any], i: int) -> dict[str, float]:
    f = {k: float(ser[k][i]) for k in ("er", "adx", "disp", "atrx", "sd")}
    f["abs_disp"] = abs(f["disp"])
    trend, vol = trailing_regime(ser["close"][:i + 1])  # the bot's own rule (ml/feature_engineering.py)
    f["ml_trend"], f["ml_vol"] = float(trend), float(vol)
    return f


def _forward(ser: Mapping[str, Any], i: int, h: int) -> np.ndarray | None:
    starts, idx = ser["starts"], ser["index"]
    end = idx.get(_minute(starts[i], h))
    if end is None or end - i != h:
        return None  # a minute is missing in the window
    return np.asarray(ser["close"][i:end + 1], dtype=np.float64)


def _outcomes(path: np.ndarray, direction: int, sd: float, implied: float | None, h: int) -> dict[str, float]:
    r = np.diff(np.log(path))
    rv = float(np.sum(r * r))
    fwd = float(math.log(path[-1] / path[0]))
    out = {"fwd_ret": fwd, "cont": fwd * direction / (sd * math.sqrt(h)) if sd > 0 and direction else math.nan,
           "log_rv_trailing": math.log(rv / (sd * sd * h)) if rv > 0 and sd > 0 else math.nan,
           "log_rv_implied": math.log(rv / implied) if rv > 0 and implied else math.nan}
    return out


def regime_at(day: date, t0: datetime, vix_now: float | None, daily: DailyHistory, expiries: set[date],
            s: SampleSettings) -> dict[str, Any]:
    nifty_before, vix_before = daily.before(day)
    pred = classify(vix_now=vix_now or 0.0, vix_closes_before=vix_before, nifty_closes_before=nifty_before,
                    open_today=daily.open_of(day), expiry_day=day in expiries, s=s.regime)
    return {"trend": pred.trend_label if len(set(pred.trend.values())) > 1 else None,
            "vol": pred.vol_label if len(set(pred.vol.values())) > 1 else None,
            "vol_change": pred.extra["vol_change"], "named": named(pred), "expiry_day": pred.expiry_day,
            "confidence": pred.extra["confidence"], "reasons": pred.extra["reasons"]}


def build_samples(day_bars: Mapping[date, Mapping[time, Bar]], vix_minutes: Mapping[date, Mapping[time, float]],
                  daily: DailyHistory, shares: Mapping[date, float], expiries: set[date], s: SampleSettings,
                  *, tz: Any, default_share: float = 0.35) -> list[Sample]:
    out: list[Sample] = []
    clock = vix_clock()
    for day in sorted(day_bars):
        bars = day_bars[day]
        if len(bars) < s.lookback + s.horizon:
            continue
        ser = session_series(bars, s)
        vix_day = vix_minutes.get(day, {})
        w = shares.get(day, default_share)
        jobs: list[tuple[str, time, int, dict[str, Any]]] = []
        for cp in s.checkpoints:
            i = ser["index"].get(_minute(cp, -1))
            if i is not None:
                d = ser["disp"][i]
                jobs.append(("CHECKPOINT", cp, 0 if not math.isfinite(d) or d == 0 else (1 if d > 0 else -1), {}))
        for sig in orb_signals(day, bars, s.orb, tz):
            jobs.append(("ORB", sig.candle_end.time(), 1 if sig.option_type == "CE" else -1,
                         {"option_type": sig.option_type, "or_high": sig.or_high, "or_low": sig.or_low}))
        for kind, end_t, direction, info in jobs:
            i = ser["index"].get(_minute(end_t, -1))  # the bar ENDING at the sample time
            if i is None:
                continue
            path = _forward(ser, i, s.horizon)
            if path is None:
                continue
            f = features_at(ser, i)
            vix_now = vix_day.get(end_t)
            implied = window_variance(vix_now / 100, s.horizon, "business", clock=clock, overnight_share=w) \
                if vix_now else None
            t0 = datetime.combine(day, end_t, tzinfo=tz)
            out.append(Sample(kind, day, t0, t0 + timedelta(minutes=s.horizon), direction, f,
                              regime_at(day, t0, vix_now, daily, expiries, s),
                              _outcomes(path, direction, f["sd"], implied, s.horizon), info))
    return out


__all__ = ["Bar", "DailyHistory", "Sample", "SampleSettings", "build_samples", "features_at", "regime_at",
           "session_series"]
