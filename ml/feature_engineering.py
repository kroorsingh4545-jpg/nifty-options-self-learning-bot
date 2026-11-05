"""Point-in-time feature engineering (feature schema v1).

ONE function, ``compute_features``, produces every feature, for training
(batch, over a historical dataset) and for live inference (``FeatureEngine``,
fed by the bot's completed 1-minute candles). Training and serving cannot
drift apart because they run the same code.

Point-in-time rules, enforced structurally:

* A decision is made at ``as_of`` (a signal's ``candle_end``). Only 1-minute
  bars whose END is at or before ``as_of`` are visible: ``compute_features``
  slices its inputs with ``Bars.upto(as_of)`` itself, so a caller that passes
  the whole day cannot leak later bars into a feature.
* Intraday features use the current session only (no overnight mixing); the
  previous session contributes only its last close (``gap_pct``).
* Option context (premium, OI, volume, IV, spread) must be stamped at or
  before ``as_of``; a later quote raises ``LookaheadError``.
* Recent trade outcomes count only trades CLOSED at or before ``as_of``.
* Regime features are rule-based on TRAILING data only. The ML regime
  classifier (forward-looking labels) is a separate model and is never a
  training feature here.
* No feature is normalised with dataset-wide statistics: every value is a
  ratio, percentage or count computed from the trailing window, so there is
  no scaler to fit and nothing from a test window can leak into training.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt

from config.settings import MarketSession
from core.clock import IST, ensure_ist
from core.enums import OptionType
from core.models import Candle
from options.pricing import YEAR_SECONDS, implied_volatility

FEATURE_SCHEMA_VERSION = "1.0.0"
MINUTES_PER_SESSION = 375  # 09:15-15:30
ANNUALISE_1M = math.sqrt(MINUTES_PER_SESSION * 252)  # 1-minute return std -> annualised volatility
NAN = float("nan")

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


class LookaheadError(ValueError):
    """An input is stamped after the decision time (would leak the future)."""


# --------------------------------------------------------------------------- #
# Feature dictionary
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    unit: str
    description: str
    lookback: str


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec("direction", "signal", "+1/-1", "+1 for a CE (bullish) setup, -1 for PE, 0 when undirected", "none"),
    FeatureSpec("ret_1m", "returns", "%", "NIFTY log return over the last 1 completed minute", "1 bar"),
    FeatureSpec("ret_3m", "returns", "%", "NIFTY log return over the last 3 completed minutes", "3 bars"),
    FeatureSpec("ret_5m", "returns", "%", "NIFTY log return over the last 5 completed minutes", "5 bars"),
    FeatureSpec("ret_15m", "returns", "%", "NIFTY log return over the last 15 completed minutes", "15 bars"),
    FeatureSpec("ret_30m", "returns", "%", "NIFTY log return over the last 30 completed minutes", "30 bars"),
    FeatureSpec("gap_pct", "returns", "%", "session open vs previous session's last close", "previous close"),
    FeatureSpec("vwap_dist_pct", "trend", "%", "NIFTY futures close vs its session VWAP (index has no volume)",
                "session"),
    FeatureSpec("ema20_dist_pct", "trend", "%", "NIFTY close vs EMA(20) of 1-minute closes", "session, >=20 bars"),
    FeatureSpec("ema20_slope_pct", "trend", "%", "change of EMA(20) over the last 5 minutes, % of EMA",
                "session, >=25 bars"),
    FeatureSpec("atr14_pct", "volatility", "%", "mean 1-minute true range over 14 bars, % of close", "14 bars"),
    FeatureSpec("rv_15m", "volatility", "annualised", "realised volatility of 1-minute log returns, 15 bars",
                "15 returns"),
    FeatureSpec("rv_30m", "volatility", "annualised", "realised volatility of 1-minute log returns, 30 bars",
                "30 returns"),
    FeatureSpec("or_position", "structure", "ratio", "(close - OR low) / (OR high - OR low); <0 below, >1 above",
                "opening range, after it completes"),
    FeatureSpec("or_width_pct", "structure", "%", "opening-range width, % of OR low", "opening range"),
    FeatureSpec("minutes_since_open", "time", "minutes", "minutes from the session open to as-of", "clock"),
    FeatureSpec("minutes_to_square_off", "time", "minutes", "minutes from as-of to the square-off start", "clock"),
    FeatureSpec("day_of_week", "time", "0-4", "Monday=0 ... Friday=4", "clock"),
    FeatureSpec("minutes_to_expiry", "expiry", "minutes", "minutes from as-of to the option expiry close",
                "calendar"),
    FeatureSpec("is_expiry_day", "expiry", "0/1", "1 on the option's expiry day", "calendar"),
    FeatureSpec("option_premium", "option", "rupees", "last traded premium of the selected contract", "<= as-of"),
    FeatureSpec("option_premium_pct_spot", "option", "%", "premium as % of NIFTY spot", "<= as-of"),
    FeatureSpec("option_moneyness_pct", "option", "%", "direction * (spot - strike) / spot; >0 in the money",
                "<= as-of"),
    FeatureSpec("option_iv", "option", "annualised", "implied volatility: from the feed, else solved from the "
                "premium (Black-Scholes)", "<= as-of"),
    FeatureSpec("option_volume_log", "option", "log1p", "log(1 + day volume so far) of the contract", "<= as-of"),
    FeatureSpec("option_oi_log", "option", "log1p", "log(1 + open interest) of the contract", "<= as-of"),
    FeatureSpec("option_spread_pct", "option", "%", "bid-ask spread as % of mid (NaN when no quote depth)",
                "<= as-of"),
    FeatureSpec("recent_win_rate_5", "outcomes", "ratio", "win rate of this strategy's last 5 CLOSED trades",
                "trades closed <= as-of"),
    FeatureSpec("recent_avg_r_5", "outcomes", "R", "mean R-multiple of the last 5 closed trades",
                "trades closed <= as-of"),
    FeatureSpec("consecutive_losses", "outcomes", "count", "current losing streak of closed trades",
                "trades closed <= as-of"),
    FeatureSpec("regime_trend", "regime", "-1/0/+1", "trailing trend regime: down / range-bound / up", "30 bars"),
    FeatureSpec("regime_vol", "regime", "-1/0/+1", "trailing volatility regime: low / normal / high", "30 returns"),
)
FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)


def feature_schema() -> dict[str, Any]:
    """Versioned schema stored with every model (names, order, units)."""
    specs = [asdict(f) | {"dtype": "float64"} for f in FEATURES]
    digest = hashlib.sha256(json.dumps([(f["name"], f["dtype"]) for f in specs]).encode()).hexdigest()[:16]
    return {"version": FEATURE_SCHEMA_VERSION, "hash": digest, "features": specs}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def _epoch(t: datetime) -> int:
    return int(ensure_ist(t).timestamp())


@dataclass(frozen=True)
class Bars:
    """Completed 1-minute bars of one instrument, oldest first (numpy columns)."""

    start: IntArray
    end: IntArray
    day: IntArray  # IST date ordinal of the bar start
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray
    oi: FloatArray

    @classmethod
    def empty(cls) -> Bars:
        i, f = np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
        return cls(i, i, i, f, f, f, f, f, f)

    @classmethod
    def from_candles(cls, candles: Iterable[Candle]) -> Bars:
        rows = [c for c in candles if c.timeframe_minutes == 1 and c.complete]
        if not rows:
            return cls.empty()
        start = np.array([_epoch(c.start) for c in rows], dtype=np.int64)
        if np.any(np.diff(start) <= 0):
            raise ValueError("bars must be strictly increasing in time")
        return cls(
            start=start,
            end=np.array([_epoch(c.end) for c in rows], dtype=np.int64),
            day=np.array([c.start.astimezone(IST).date().toordinal() for c in rows], dtype=np.int64),
            open=np.array([c.open for c in rows], dtype=np.float64),
            high=np.array([c.high for c in rows], dtype=np.float64),
            low=np.array([c.low for c in rows], dtype=np.float64),
            close=np.array([c.close for c in rows], dtype=np.float64),
            volume=np.array([c.volume for c in rows], dtype=np.float64),
            oi=np.array([NAN if c.oi is None else c.oi for c in rows], dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.start.size)

    def _take(self, sl: slice) -> Bars:
        return Bars(self.start[sl], self.end[sl], self.day[sl], self.open[sl], self.high[sl], self.low[sl],
                    self.close[sl], self.volume[sl], self.oi[sl])

    def upto(self, as_of: datetime) -> Bars:
        """Only bars completed at or before ``as_of`` (the point-in-time view)."""
        return self._take(slice(0, int(np.searchsorted(self.end, _epoch(as_of), side="right"))))

    def session(self, d: date) -> Bars:
        o = d.toordinal()
        return self._take(slice(int(np.searchsorted(self.day, o, side="left")),
                                int(np.searchsorted(self.day, o, side="right"))))

    def before_day(self, d: date) -> Bars:
        return self._take(slice(0, int(np.searchsorted(self.day, d.toordinal(), side="left"))))


@dataclass(frozen=True)
class OptionContext:
    """The selected contract as seen at ``as_of`` (a quote or a completed bar)."""

    option_type: OptionType
    strike: float
    expiry: date
    as_of: datetime
    premium: float | None = None
    day_volume: float | None = None
    oi: float | None = None
    bid: float | None = None
    ask: float | None = None
    iv: float | None = None  # from the feed when available


@dataclass(frozen=True)
class PastOutcome:
    closed_at: datetime
    net_pnl: float
    r_multiple: float | None = None

    @property
    def win(self) -> bool:
        return self.net_pnl > 0


@dataclass(frozen=True)
class RegimeThresholds:
    """Rule-based regime cut-offs (annualised realised volatility; trend =
    30-minute move in units of its expected size at the trailing volatility)."""

    low_vol: float = 0.09
    high_vol: float = 0.16
    trend_sigmas: float = 1.0
    window: int = 30

    def __post_init__(self) -> None:
        if not 0 < self.low_vol < self.high_vol:
            raise ValueError("need 0 < low_vol < high_vol")
        if self.trend_sigmas <= 0 or self.window < 5:
            raise ValueError("trend_sigmas must be > 0 and window >= 5")


DEFAULT_THRESHOLDS = RegimeThresholds()


@dataclass(frozen=True)
class SessionTimes:
    open: time = time(9, 15)
    close: time = time(15, 30)
    square_off_start: time = time(15, 10)
    opening_range_end: time = time(9, 30)
    rate: float = 0.065  # risk-free rate for implied volatility

    @classmethod
    def from_config(cls, session: MarketSession, square_off_start: time, opening_range_end: time = time(9, 30),
                    rate: float = 0.065) -> SessionTimes:
        return cls(session.market_open, session.market_close, square_off_start, opening_range_end, rate)


# --------------------------------------------------------------------------- #
# Building blocks (all on trailing arrays)
# --------------------------------------------------------------------------- #


def _log_return_pct(close: FloatArray, n: int) -> float:
    if close.size < n + 1:
        return NAN
    return float(math.log(close[-1] / close[-1 - n]) * 100)


def _realised_vol(close: FloatArray, n: int) -> float:
    if close.size < n + 1:
        return NAN
    rets = np.diff(np.log(close[-(n + 1):]))
    return float(np.std(rets, ddof=1) * ANNUALISE_1M)


def _ema_series(close: FloatArray, period: int) -> FloatArray:
    """EMA seeded with the SMA of the first ``period`` closes (same as strategies.indicators.EMA)."""
    out = np.full(close.size, NAN)
    if close.size < period:
        return out
    alpha = 2.0 / (period + 1)
    value = float(np.mean(close[:period]))
    out[period - 1] = value
    for i in range(period, close.size):
        value += alpha * (float(close[i]) - value)
        out[i] = value
    return out


def _mean_true_range(b: Bars, n: int) -> float:
    if len(b) < n:
        return NAN
    hi, lo, cl = b.high[-n:], b.low[-n:], b.close
    prev = cl[-n - 1:-1] if cl.size > n else np.concatenate(([b.open[-n]], cl[-n:-1]))
    tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev), np.abs(lo - prev)))
    return float(np.mean(tr))


def trailing_regime(close: FloatArray, thresholds: RegimeThresholds = DEFAULT_THRESHOLDS) -> tuple[float, float]:
    """(trend, vol) in {-1, 0, +1} from the trailing ``window`` bars; NaN when too short."""
    n = thresholds.window
    rv = _realised_vol(close, n)
    if math.isnan(rv):
        return NAN, NAN
    vol = -1.0 if rv < thresholds.low_vol else (1.0 if rv > thresholds.high_vol else 0.0)
    move = _log_return_pct(close, n) / 100
    expected = rv / ANNUALISE_1M * math.sqrt(n)
    if expected <= 0:
        return 0.0, vol
    z = move / expected
    trend = 1.0 if z > thresholds.trend_sigmas else (-1.0 if z < -thresholds.trend_sigmas else 0.0)
    return trend, vol


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _session_vwap(b: Bars) -> float:
    vol = float(np.sum(b.volume))
    if vol <= 0:
        return NAN
    typical = (b.high + b.low + b.close) / 3
    return float(np.sum(typical * b.volume) / vol)


def outcome_features(outcomes: Sequence[PastOutcome], as_of: datetime, n: int = 5) -> dict[str, float]:
    done = sorted((o for o in outcomes if ensure_ist(o.closed_at) <= as_of), key=lambda o: o.closed_at)
    if not done:
        return {"recent_win_rate_5": NAN, "recent_avg_r_5": NAN, "consecutive_losses": 0.0}
    last = done[-n:]
    rs = [o.r_multiple for o in last if o.r_multiple is not None]
    streak = 0
    for o in reversed(done):
        if o.win:
            break
        streak += 1
    return {"recent_win_rate_5": sum(o.win for o in last) / len(last),
            "recent_avg_r_5": float(np.mean(rs)) if rs else NAN, "consecutive_losses": float(streak)}


def option_features(option: OptionContext | None, spot: float, as_of: datetime,
                    times: SessionTimes) -> dict[str, float]:
    out = {k: NAN for k in ("option_premium", "option_premium_pct_spot", "option_moneyness_pct", "option_iv",
                            "option_volume_log", "option_oi_log", "option_spread_pct", "minutes_to_expiry",
                            "is_expiry_day")}
    if option is None:
        return out
    if ensure_ist(option.as_of) > as_of:
        raise LookaheadError(f"option context stamped {option.as_of.isoformat()} is after as-of {as_of.isoformat()}")
    expiry_close = datetime.combine(option.expiry, times.close, tzinfo=IST)
    out["minutes_to_expiry"] = max(0.0, _minutes(as_of, expiry_close))
    out["is_expiry_day"] = 1.0 if as_of.astimezone(IST).date() == option.expiry else 0.0
    sign = 1.0 if option.option_type is OptionType.CE else -1.0
    if spot > 0:
        out["option_moneyness_pct"] = sign * (spot - option.strike) / spot * 100
    p = option.premium
    if p is not None and p > 0:
        out["option_premium"] = p
        if spot > 0:
            out["option_premium_pct_spot"] = p / spot * 100
        iv = option.iv
        if iv is None and spot > 0:
            years = max((expiry_close - as_of).total_seconds(), 60.0) / YEAR_SECONDS
            iv = implied_volatility(p, spot, option.strike, years, times.rate, option.option_type is OptionType.CE)
        if iv is not None and iv > 0:
            out["option_iv"] = float(iv)
    if option.day_volume is not None and option.day_volume >= 0:
        out["option_volume_log"] = math.log1p(option.day_volume)
    if option.oi is not None and option.oi >= 0:
        out["option_oi_log"] = math.log1p(option.oi)
    if option.bid is not None and option.ask is not None and option.bid > 0 and option.ask >= option.bid:
        mid = (option.bid + option.ask) / 2
        out["option_spread_pct"] = (option.ask - option.bid) / mid * 100
    return out


# --------------------------------------------------------------------------- #
# The feature function
# --------------------------------------------------------------------------- #


def compute_features(
    index_bars: Bars,
    as_of: datetime,
    *,
    times: SessionTimes,
    direction: OptionType | None = None,
    futures_bars: Bars | None = None,
    option: OptionContext | None = None,
    outcomes: Sequence[PastOutcome] = (),
    thresholds: RegimeThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, float]:
    """Every schema-v1 feature as of ``as_of``, in ``FEATURE_NAMES`` order.
    Missing inputs give NaN (LightGBM handles missing values natively)."""
    as_of = ensure_ist(as_of)
    day = as_of.astimezone(IST).date()
    visible = index_bars.upto(as_of)  # point-in-time: nothing after as_of
    today = visible.session(day)
    close = today.close
    f: dict[str, float] = dict.fromkeys(FEATURE_NAMES, NAN)
    f["direction"] = 0.0 if direction is None else (1.0 if direction is OptionType.CE else -1.0)

    for n in (1, 3, 5, 15, 30):
        f[f"ret_{n}m"] = _log_return_pct(close, n)
    prev = visible.before_day(day)
    if len(prev) and len(today):
        f["gap_pct"] = float((today.open[0] / prev.close[-1] - 1) * 100)

    spot = float(close[-1]) if close.size else NAN
    if close.size:
        ema = _ema_series(close, 20)
        if not math.isnan(ema[-1]):
            f["ema20_dist_pct"] = float((spot - ema[-1]) / ema[-1] * 100)
            if close.size >= 25 and not math.isnan(ema[-6]):
                f["ema20_slope_pct"] = float((ema[-1] - ema[-6]) / ema[-1] * 100)
        atr = _mean_true_range(today, 14)
        if not math.isnan(atr):
            f["atr14_pct"] = atr / spot * 100
        f["rv_15m"] = _realised_vol(close, 15)
        f["rv_30m"] = _realised_vol(close, 30)
        f["regime_trend"], f["regime_vol"] = trailing_regime(close, thresholds)

        or_end = datetime.combine(day, times.opening_range_end, tzinfo=IST)
        if as_of >= or_end:
            orb = today.upto(or_end)
            if len(orb):
                hi, lo = float(np.max(orb.high)), float(np.min(orb.low))
                f["or_width_pct"] = (hi - lo) / lo * 100
                f["or_position"] = (spot - lo) / (hi - lo) if hi > lo else NAN

    if futures_bars is not None:
        fut = futures_bars.upto(as_of).session(day)
        if len(fut):
            vwap = _session_vwap(fut)
            if not math.isnan(vwap):
                f["vwap_dist_pct"] = float((fut.close[-1] - vwap) / vwap * 100)

    open_dt = datetime.combine(day, times.open, tzinfo=IST)
    f["minutes_since_open"] = max(0.0, _minutes(open_dt, as_of))
    f["minutes_to_square_off"] = _minutes(as_of, datetime.combine(day, times.square_off_start, tzinfo=IST))
    f["day_of_week"] = float(day.weekday())
    f.update(option_features(option, spot if not math.isnan(spot) else 0.0, as_of, times))
    f.update(outcome_features(outcomes, as_of))
    return {k: float(f[k]) for k in FEATURE_NAMES}


def feature_vector(features: dict[str, float]) -> FloatArray:
    """Features in schema order; refuses a dict that is not exactly schema v1."""
    if set(features) != set(FEATURE_NAMES):
        missing, extra = set(FEATURE_NAMES) - set(features), set(features) - set(FEATURE_NAMES)
        raise ValueError(f"feature set does not match schema {FEATURE_SCHEMA_VERSION}: missing {sorted(missing)}, "
                         f"unexpected {sorted(extra)}")
    return np.array([features[k] for k in FEATURE_NAMES], dtype=np.float64)


# --------------------------------------------------------------------------- #
# Live (incremental) engine
# --------------------------------------------------------------------------- #


@dataclass
class _Series:
    candles: list[Candle] = field(default_factory=list)
    _cache: Bars | None = None

    def add(self, c: Candle) -> None:
        if self.candles and c.start <= self.candles[-1].start:
            return  # duplicates / out-of-order bars are ignored, never reordered
        self.candles.append(c)
        self._cache = None

    def bars(self) -> Bars:
        if self._cache is None:
            self._cache = Bars.from_candles(self.candles)
        return self._cache

    def trim_before(self, d: date) -> None:
        """Keep the last bar of earlier sessions (for gap_pct) and today's bars."""
        old = [c for c in self.candles if c.start.astimezone(IST).date() < d]
        new = [c for c in self.candles if c.start.astimezone(IST).date() >= d]
        self.candles = old[-1:] + new
        self._cache = None


class FeatureEngine:
    """Live feature state for inference: feed completed 1-minute candles of
    the NIFTY index (and the current NIFTY future, for VWAP) with
    ``on_candle``; ask for ``features(as_of, ...)``. Uses ``compute_features``,
    exactly as training does."""

    def __init__(self, *, index_key: str, times: SessionTimes, thresholds: RegimeThresholds = DEFAULT_THRESHOLDS,
                 futures_key: str | None = None) -> None:
        self.index_key = index_key
        self.futures_key = futures_key
        self.times = times
        self.thresholds = thresholds
        self._index = _Series()
        self._futures = _Series()
        self._day: date | None = None

    def set_futures_key(self, key: str | None) -> None:
        if key != self.futures_key:
            self.futures_key = key
            self._futures = _Series()

    def on_candle(self, candle: Candle) -> None:
        if candle.timeframe_minutes != 1 or not candle.complete:
            return
        d = candle.start.astimezone(IST).date()
        if self._day is None or d > self._day:
            self._day = d
            self._index.trim_before(d)
            self._futures.trim_before(d)
        if candle.instrument_key == self.index_key:
            self._index.add(candle)
        elif self.futures_key is not None and candle.instrument_key == self.futures_key:
            self._futures.add(candle)

    @property
    def last_bar_end(self) -> datetime | None:
        return self._index.candles[-1].end if self._index.candles else None

    def session_open(self, d: date) -> float | None:
        """Open of the session's first NIFTY index bar."""
        s = self._index.bars().session(d)
        return float(s.open[0]) if len(s) else None

    def last_close(self, as_of: datetime) -> float | None:
        """NIFTY index close of the last bar completed by ``as_of`` (same session)."""
        b = self._index.bars().upto(as_of).session(ensure_ist(as_of).astimezone(IST).date())
        return float(b.close[-1]) if len(b) else None

    def features(self, as_of: datetime, *, direction: OptionType | None = None, option: OptionContext | None = None,
                 outcomes: Sequence[PastOutcome] = ()) -> dict[str, float]:
        fut = self._futures.bars() if self.futures_key is not None else None
        return compute_features(self._index.bars(), as_of, times=self.times, direction=direction, futures_bars=fut,
                                option=option, outcomes=outcomes, thresholds=self.thresholds)


def bars_stale(last_bar_end: datetime | None, as_of: datetime, max_gap: timedelta = timedelta(minutes=2)) -> bool:
    """True when the newest visible bar is older than ``max_gap`` at ``as_of`` (feed gap)."""
    return last_bar_end is None or ensure_ist(as_of) - ensure_ist(last_bar_end) > max_gap


__all__ = ["FEATURES", "FEATURE_NAMES", "FEATURE_SCHEMA_VERSION", "Bars", "FeatureEngine", "FeatureSpec",
           "LookaheadError", "OptionContext", "PastOutcome", "RegimeThresholds", "SessionTimes", "bars_stale",
           "compute_features", "feature_schema", "feature_vector", "outcome_features", "trailing_regime"]
