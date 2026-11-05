"""Gymnasium environment for the RL trade manager (Phase 8).

The agent MANAGES trades the strategies propose; it does not speculate. One
episode is one trading day of one strategy, replayed from completed 1-minute
bars (point in time: an observation at ``t`` uses only bars that ended at or
before ``t``).

Decision points
---------------
* **Setup**: a strategy signal (or, for research, a grid candidate) whose
  contract passes the strategy's strike rules and liquidity bounds, AND which
  the risk envelope would allow right now. The agent answers ENTER or
  SKIP_SETUP. Setups the envelope blocks (entry window, 14:45 cut-off, daily
  trade cap, cooldown, consecutive-loss pause, daily-loss halt, zero size)
  are never shown to the agent.
* **Position**: every ``rl.decision_interval_minutes`` while a trade is
  open. The agent answers HOLD, TIGHTEN_STOP, MOVE_STOP_TO_BREAKEVEN,
  TRAIL_STOP, TAKE_PARTIAL_PROFIT or EXIT_FULL.

Execution model (no lookahead): an action decided at bar end ``t`` executes
at the OPEN of the option bar that starts at ``t``, with adverse slippage of
``paper_broker.slippage_ticks`` ticks. Stops and targets are then checked
bar by bar with the backtester's path convention (an up-bar visits its low
first; a bar opening through a level fills at the open).

Risk envelope (NOT controlled by the agent)
-------------------------------------------
``RiskEnvelope`` is built from ``config/risk_limits.yaml`` and the strategy
config and is frozen. It sizes every entry (risk budget / stop distance,
``risk.position_sizer`` rules), keeps the initial stop and target from the
strategy's rules, allows only one managed trade at a time, blocks entries
outside the window / after 14:45 / over the daily cap / in the cooldown /
after the consecutive-loss limit / after the daily-loss limit, forces an exit
when the day's loss reaches the daily limit, and squares off at the
square-off start (15:10). The action space has no action that can change any
of these. Stop actions can only TIGHTEN (``proposed_stop`` never returns a
looser stop, and never one at or above the market).

Everything here also runs live: ``build_observation`` and the action rules
are shared with ``ml.inference_service.RLTradeAdvisor``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import IntEnum
from typing import Any, Literal

import gymnasium as gym
import numpy as np
import numpy.typing as npt
from gymnasium import spaces

from backtest.data import HistoricalDataset
from broker.charges import ChargeCalculator
from config.settings import BotConfig, RLConfig
from core.clock import IST, ensure_ist
from core.enums import OptionType, OrderSide
from core.models import Instrument
from ml.dataset import OptionHistory, futures_bars, grid_events, liquidity_reasons, session_times, strategy_events
from ml.feature_engineering import (
    FEATURE_SCHEMA_VERSION,
    Bars,
    OptionContext,
    PastOutcome,
    SessionTimes,
    compute_features,
    feature_vector,
    option_features,
    outcome_features,
)
from ml.regime_classifier import RegimePrediction, RuleBasedRegime
from ml.reward_design import RewardWeights, StepOutcome, compute_reward, reward_spec
from options.pricing import YEAR_SECONDS, bs_greeks

log = logging.getLogger(__name__)

RL_ENV_VERSION = "1.0.0"
OBS_SCHEMA_VERSION = "1.0.0"
NAN = float("nan")
Float32Array = npt.NDArray[np.float32]
FloatArray = npt.NDArray[np.float64]


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


class Action(IntEnum):
    """The complete action space. There is deliberately no action for daily
    loss limits, exposure, the kill switch, credentials or square-off timing."""

    HOLD = 0
    TIGHTEN_STOP = 1
    MOVE_STOP_TO_BREAKEVEN = 2
    TRAIL_STOP = 3
    TAKE_PARTIAL_PROFIT = 4
    EXIT_FULL = 5
    ENTER = 6  # only when no position and a valid setup is offered
    SKIP_SETUP = 7


STOP_ACTIONS = frozenset({Action.TIGHTEN_STOP, Action.MOVE_STOP_TO_BREAKEVEN, Action.TRAIL_STOP})
POSITION_ACTIONS = frozenset({Action.HOLD, *STOP_ACTIONS, Action.TAKE_PARTIAL_PROFIT, Action.EXIT_FULL})
SETUP_ACTIONS = frozenset({Action.ENTER, Action.SKIP_SETUP})


def action_mask(in_position: bool, setup_offered: bool) -> npt.NDArray[np.bool_]:
    """Actions that are meaningful in the current state."""
    m = np.zeros(len(Action), dtype=bool)
    if in_position:
        m[[a.value for a in POSITION_ACTIONS]] = True
    elif setup_offered:
        m[[Action.ENTER.value, Action.SKIP_SETUP.value]] = True
    else:
        m[Action.HOLD.value] = True
    return m


def _ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def _floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 2)


# --------------------------------------------------------------------------- #
# What the agent sees (shared by the simulator and live inference)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionView:
    option_type: OptionType
    entry: float  # average entry fill
    initial_stop: float
    stop: float
    target: float | None
    lot_units: int
    lots_initial: int
    lots_open: int
    entry_time: datetime
    peak: float  # highest premium since entry
    partials_taken: int = 0

    @property
    def risk_per_unit(self) -> float:
        return self.entry - self.initial_stop


@dataclass(frozen=True)
class SetupView:
    option_type: OptionType
    ml_confidence: float | None = None


@dataclass(frozen=True)
class AccountView:
    open_positions: int
    realized_pnl_today: float  # net rupees
    max_daily_loss: float
    trades_today: int
    max_trades_per_day: int
    cooldown_active: bool
    consecutive_losses: int
    risk_budget: float  # per-trade risk budget (reward and P&L unit)


OBS_GROUPS: dict[str, tuple[str, ...]] = {
    "market": ("nifty_vs_open_pct", "ret_5m", "ret_15m", "vwap_dist_pct", "ema20_dist_pct", "ema20_slope_pct",
               "atr14_pct", "rv_15m", "rv_30m"),
    "option": ("option_premium_pct_spot", "option_moneyness_pct", "option_iv", "delta", "gamma_1pct", "theta_pct",
               "vega_pct", "minutes_to_expiry", "is_expiry_day"),
    "setup": ("setup_available", "setup_direction", "ml_confidence", "ml_available"),
    "position": ("in_position", "position_direction", "entry_premium_pct_spot", "premium_vs_entry_pct",
                 "unrealized_pnl_budget", "r_multiple", "stop_distance_r", "target_distance_r", "peak_r",
                 "time_in_trade_min", "lots_open_frac", "partials_taken", "stop_at_or_above_entry"),
    "time": ("minutes_since_open", "minutes_to_square_off", "minutes_to_entry_cutoff"),
    "regime": ("p_trend_down", "p_range", "p_trend_up", "p_vol_low", "p_vol_normal", "p_vol_high"),
    "account": ("open_positions", "daily_pnl_frac", "daily_loss_headroom_frac", "trades_today_frac",
                "cooldown_active", "consecutive_losses", "recent_win_rate_5", "recent_avg_r_5"),
}
OBS_NAMES: tuple[str, ...] = tuple(n for g in OBS_GROUPS.values() for n in g)

OBS_UNITS: dict[str, str] = {
    "nifty_vs_open_pct": "% vs session open", "ret_5m": "%", "ret_15m": "%", "vwap_dist_pct": "% (future vs VWAP)",
    "ema20_dist_pct": "%", "ema20_slope_pct": "%", "atr14_pct": "%", "rv_15m": "annualised", "rv_30m": "annualised",
    "option_premium_pct_spot": "%", "option_moneyness_pct": "% (>0 ITM)", "option_iv": "annualised",
    "delta": "per point", "gamma_1pct": "delta change per 1% NIFTY move", "theta_pct": "% of premium per day",
    "vega_pct": "% of premium per vol point", "minutes_to_expiry": "min", "is_expiry_day": "0/1",
    "setup_available": "0/1", "setup_direction": "+1 CE / -1 PE / 0", "ml_confidence": "0..1 (NaN: none)",
    "ml_available": "0/1", "in_position": "0/1", "position_direction": "+1 CE / -1 PE / 0",
    "entry_premium_pct_spot": "% (entry price)", "premium_vs_entry_pct": "%",
    "unrealized_pnl_budget": "rupees / risk budget", "r_multiple": "R (gross, current price)",
    "stop_distance_r": "R", "target_distance_r": "R", "peak_r": "R", "time_in_trade_min": "min",
    "lots_open_frac": "ratio", "partials_taken": "count", "stop_at_or_above_entry": "0/1",
    "minutes_since_open": "min", "minutes_to_square_off": "min", "minutes_to_entry_cutoff": "min",
    "p_trend_down": "probability", "p_range": "probability", "p_trend_up": "probability",
    "p_vol_low": "probability", "p_vol_normal": "probability", "p_vol_high": "probability",
    "open_positions": "count", "daily_pnl_frac": "realised net / daily loss limit",
    "daily_loss_headroom_frac": "ratio", "trades_today_frac": "ratio", "cooldown_active": "0/1",
    "consecutive_losses": "count", "recent_win_rate_5": "ratio", "recent_avg_r_5": "R",
}


def obs_schema() -> dict[str, Any]:
    body = {"version": OBS_SCHEMA_VERSION, "names": list(OBS_NAMES), "groups": {k: list(v) for k, v in
                                                                              OBS_GROUPS.items()},
            "units": OBS_UNITS}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "hash": digest}


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def build_observation(*, market: Mapping[str, float], spot: float | None, session_open_price: float | None,
                      as_of: datetime, times: SessionTimes, no_new_entries_after: time,
                      option: OptionContext | None, premium: float | None, position: PositionView | None,
                      setup: SetupView | None, account: AccountView, outcomes: Sequence[PastOutcome],
                      regime: RegimePrediction) -> dict[str, float]:
    """Raw observation (NaN = unavailable) as of ``as_of``.

    ``market`` is a schema-v1 feature dict computed as of ``as_of``;
    ``option`` / ``premium`` describe the position's contract (or the setup's
    when flat); ``outcomes`` are the strategy's own CLOSED trades."""
    as_of = ensure_ist(as_of)
    o: dict[str, float] = dict.fromkeys(OBS_NAMES, NAN)
    for k in ("ret_5m", "ret_15m", "vwap_dist_pct", "ema20_dist_pct", "ema20_slope_pct", "atr14_pct", "rv_15m",
              "rv_30m"):
        o[k] = float(market.get(k, NAN))
    if spot is not None and session_open_price and session_open_price > 0:
        o["nifty_vs_open_pct"] = (spot / session_open_price - 1) * 100
    s = spot if spot is not None and spot > 0 else 0.0
    of = option_features(option, s, as_of, times)
    for k in ("option_premium_pct_spot", "option_moneyness_pct", "option_iv", "minutes_to_expiry", "is_expiry_day"):
        o[k] = of[k]
    iv = of["option_iv"]
    if option is not None and s > 0 and premium and premium > 0 and math.isfinite(iv):
        years = max((datetime.combine(option.expiry, times.close, tzinfo=IST) - as_of).total_seconds(),
                    60.0) / YEAR_SECONDS
        g = bs_greeks(s, option.strike, years, iv, times.rate, option.option_type is OptionType.CE)
        if g is not None:
            o["delta"] = g["delta"]
            o["gamma_1pct"] = g["gamma"] * s / 100
            o["theta_pct"] = g["theta"] / premium * 100
            o["vega_pct"] = g["vega"] / premium * 100

    o["setup_available"] = 1.0 if setup is not None else 0.0
    o["setup_direction"] = 0.0 if setup is None else (1.0 if setup.option_type is OptionType.CE else -1.0)
    conf = None if setup is None else setup.ml_confidence
    o["ml_available"] = 1.0 if conf is not None and math.isfinite(conf) else 0.0
    if o["ml_available"]:
        o["ml_confidence"] = float(conf)  # type: ignore[arg-type]

    o["in_position"] = 1.0 if position is not None else 0.0
    o["position_direction"] = 0.0
    if position is not None:
        p = position
        r = p.risk_per_unit
        o["position_direction"] = 1.0 if p.option_type is OptionType.CE else -1.0
        if s > 0:
            o["entry_premium_pct_spot"] = p.entry / s * 100
        o["time_in_trade_min"] = max(0.0, _minutes(ensure_ist(p.entry_time), as_of))
        o["lots_open_frac"] = p.lots_open / p.lots_initial if p.lots_initial > 0 else NAN
        o["partials_taken"] = float(p.partials_taken)
        o["stop_at_or_above_entry"] = 1.0 if p.stop >= p.entry - 1e-9 else 0.0
        if r > 0:
            o["peak_r"] = (p.peak - p.entry) / r
        if premium is not None and premium > 0:
            o["premium_vs_entry_pct"] = (premium / p.entry - 1) * 100
            o["unrealized_pnl_budget"] = (premium - p.entry) * p.lots_open * p.lot_units / account.risk_budget
            if r > 0:
                o["r_multiple"] = (premium - p.entry) / r
                o["stop_distance_r"] = (premium - p.stop) / r
                if p.target is not None:
                    o["target_distance_r"] = (p.target - premium) / r

    day = as_of.astimezone(IST).date()
    o["minutes_since_open"] = max(0.0, _minutes(datetime.combine(day, times.open, tzinfo=IST), as_of))
    o["minutes_to_square_off"] = _minutes(as_of, datetime.combine(day, times.square_off_start, tzinfo=IST))
    o["minutes_to_entry_cutoff"] = _minutes(as_of, datetime.combine(day, no_new_entries_after, tzinfo=IST))

    td, rg, tu = (regime.trend.get(k, NAN) for k in ("TRENDING_DOWN", "RANGE_BOUND", "TRENDING_UP"))
    vl, vn, vh = (regime.vol.get(k, NAN) for k in ("LOW_VOL", "NORMAL_VOL", "HIGH_VOL"))
    o.update(p_trend_down=td, p_range=rg, p_trend_up=tu, p_vol_low=vl, p_vol_normal=vn, p_vol_high=vh)

    a = account
    o["open_positions"] = float(a.open_positions)
    o["daily_pnl_frac"] = a.realized_pnl_today / a.max_daily_loss
    o["daily_loss_headroom_frac"] = max(0.0, a.max_daily_loss + min(0.0, a.realized_pnl_today)) / a.max_daily_loss
    o["trades_today_frac"] = a.trades_today / a.max_trades_per_day
    o["cooldown_active"] = 1.0 if a.cooldown_active else 0.0
    o["consecutive_losses"] = float(a.consecutive_losses)
    of2 = outcome_features(outcomes, as_of)
    o["recent_win_rate_5"] = of2["recent_win_rate_5"]
    o["recent_avg_r_5"] = of2["recent_avg_r_5"]
    return {k: float(o[k]) for k in OBS_NAMES}


def observation_vector(obs: Mapping[str, float]) -> FloatArray:
    if set(obs) != set(OBS_NAMES):
        raise ValueError(f"observation does not match RL schema {OBS_SCHEMA_VERSION}")
    return np.array([obs[k] for k in OBS_NAMES], dtype=np.float64)


class NormalizerError(ValueError):
    pass


@dataclass(frozen=True)
class ObservationNormalizer:
    """Per-feature standardisation fitted on TRAINING-period observations only
    and frozen afterwards (saved with the model). NaN -> 0 (the training mean)
    after scaling; values are clipped to +/- ``clip``."""

    mean: FloatArray
    std: FloatArray
    count: int
    clip: float = 10.0
    schema_hash: str = field(default_factory=lambda: obs_schema()["hash"])

    @classmethod
    def fit(cls, rows: FloatArray, clip: float = 10.0) -> ObservationNormalizer:
        x = np.asarray(rows, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != len(OBS_NAMES) or x.shape[0] < 2:
            raise NormalizerError(f"need >= 2 rows of {len(OBS_NAMES)} observations")
        with warnings.catch_warnings():  # all-NaN columns (e.g. no ML model) are expected
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(x, axis=0)
            std = np.nanstd(x, axis=0)
        mean = np.where(np.isfinite(mean), mean, 0.0)
        std = np.where(np.isfinite(std) & (std > 1e-8), std, 1.0)
        return cls(mean, std, int(x.shape[0]), clip)

    def transform(self, x: FloatArray) -> Float32Array:
        z = (np.asarray(x, dtype=np.float64) - self.mean) / self.std
        z = np.where(np.isfinite(z), z, 0.0)
        return np.clip(z, -self.clip, self.clip).astype(np.float32)

    def to_dict(self) -> dict[str, Any]:
        return {"names": list(OBS_NAMES), "mean": [float(v) for v in self.mean], "std": [float(v) for v in self.std],
                "count": self.count, "clip": self.clip, "schema_hash": self.schema_hash,
                "schema_version": OBS_SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ObservationNormalizer:
        if list(d.get("names", [])) != list(OBS_NAMES) or d.get("schema_hash") != obs_schema()["hash"]:
            raise NormalizerError("normalizer was fitted for a different observation schema")
        mean, std = np.array(d["mean"], dtype=np.float64), np.array(d["std"], dtype=np.float64)
        if mean.shape != (len(OBS_NAMES),) or std.shape != mean.shape or not np.all(std > 0):
            raise NormalizerError("invalid normalizer statistics")
        return cls(mean, std, int(d["count"]), float(d["clip"]), str(d["schema_hash"]))


# --------------------------------------------------------------------------- #
# Action rules (bounded; stops only tighten)
# --------------------------------------------------------------------------- #


def proposed_stop(action: Action, pos: PositionView, price: float, rules: RLConfig, tick: float) -> float | None:
    """The new stop a stop action asks for, or ``None`` when it would not
    tighten. Never looser than the current stop, never at/above the market."""
    if action not in STOP_ACTIONS or not price > 0:
        return None
    ceiling = _floor_tick(price, tick) - tick  # strictly below the market
    r = pos.risk_per_unit
    if action is Action.TIGHTEN_STOP:
        cand = _ceil_tick(pos.stop + rules.tighten_fraction * (price - pos.stop), tick)
    elif action is Action.MOVE_STOP_TO_BREAKEVEN:
        cand = _ceil_tick(pos.entry, tick)
        if cand > ceiling + 1e-9:
            return None  # price is not far enough above entry
    else:
        if r <= 0:
            return None
        cand = _ceil_tick(price - rules.trail_distance_r * r, tick)
    cand = round(min(cand, ceiling), 2)
    return cand if cand > pos.stop + 1e-9 else None


def partial_lots(pos: PositionView, price: float, rules: RLConfig) -> int | None:
    """Lots a TAKE_PARTIAL_PROFIT sells, or ``None`` when not allowed (at least
    one lot always stays; only in profit; at most ``max_partial_exits``)."""
    r = pos.risk_per_unit
    if pos.partials_taken >= rules.max_partial_exits or pos.lots_open < 2 or r <= 0:
        return None
    if (price - pos.entry) / r < rules.min_profit_r_for_partial:
        return None
    n = max(1, math.floor(pos.lots_open * rules.partial_fraction))
    return min(n, pos.lots_open - 1)


# --------------------------------------------------------------------------- #
# Risk envelope (fixed by config; the agent cannot change it)
# --------------------------------------------------------------------------- #


@dataclass
class DayState:
    day: date
    realized_net: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    halted: str | None = None
    outcomes: list[PastOutcome] = field(default_factory=list)

    @property
    def loss(self) -> float:
        return max(0.0, -self.realized_net)


@dataclass(frozen=True)
class RiskEnvelope:
    risk_per_trade: float
    size_multiplier: float
    max_daily_loss: float
    max_trades_per_day: int
    loss_cooldown_minutes: int
    max_consecutive_losses: int
    max_open_trades: int
    entry_start: time
    entry_end: time
    no_new_entries_after: time
    square_off_start: time

    @classmethod
    def from_config(cls, cfg: BotConfig, strategy_id: str) -> RiskEnvelope:
        s = cfg.strategies.strategies[strategy_id]
        ps, rk = cfg.risk.per_strategy, cfg.risk
        return cls(
            risk_per_trade=min(rk.per_trade.max_risk_per_trade, s.sizing.risk_per_trade),
            size_multiplier=s.sizing.size_multiplier,
            max_daily_loss=min(s.risk.max_daily_loss, ps.max_daily_loss, rk.global_.max_daily_loss),
            max_trades_per_day=min(s.risk.max_trades_per_day, ps.max_trades_per_day),
            loss_cooldown_minutes=max(s.risk.loss_cooldown_minutes, ps.loss_cooldown_minutes),
            max_consecutive_losses=min(s.risk.max_consecutive_losses, ps.max_consecutive_losses),
            max_open_trades=min(s.risk.max_open_trades, ps.max_open_trades, rk.global_.max_open_trades),
            entry_start=s.entry_window.start, entry_end=s.entry_window.end,
            no_new_entries_after=rk.time_rules.no_new_entries_after,
            square_off_start=rk.time_rules.square_off_start)

    def remaining_budget(self, d: DayState) -> float:
        return max(0.0, min(self.risk_per_trade, self.max_daily_loss - d.loss))

    def cooldown_active(self, at: datetime, d: DayState) -> bool:
        return d.last_loss_at is not None and at < d.last_loss_at + timedelta(minutes=self.loss_cooldown_minutes)

    def entry_block_reason(self, at: datetime, d: DayState, open_trades: int) -> str | None:
        t = ensure_ist(at).astimezone(IST).time()
        if d.halted:
            return f"halted: {d.halted}"
        if not self.entry_start <= t <= self.entry_end:
            return "outside the strategy entry window"
        if t >= self.no_new_entries_after:
            return f"no new entries at/after {self.no_new_entries_after:%H:%M}"
        if t >= self.square_off_start:
            return "square-off window"
        if open_trades >= 1:  # the RL environment manages one trade at a time (<= max_open_trades)
            return "a trade is already open"
        if d.trades >= self.max_trades_per_day:
            return "daily trade cap reached"
        if d.consecutive_losses >= self.max_consecutive_losses:
            return "consecutive-loss pause"
        if self.cooldown_active(at, d):
            return "loss cooldown"
        if self.remaining_budget(d) <= 0:
            return "no daily loss budget left"
        return None

    def lots(self, entry: float, stop: float, lot_units: int, exit_slippage: float, budget: float) -> int:
        """Whole lots whose loss at the stop (plus exit slippage) fits the budget."""
        per_unit = entry - stop + exit_slippage
        if per_unit <= 0 or lot_units < 1:
            return 0
        return max(0, math.floor(budget * self.size_multiplier / (per_unit * lot_units) + 1e-9))

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.strftime("%H:%M") if isinstance(v, time) else v) for k, v in self.__dict__.items()}


# --------------------------------------------------------------------------- #
# Market replay (point-in-time data for one strategy)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Setup:
    as_of: datetime
    option_type: OptionType
    instrument: Instrument
    ml_confidence: float | None
    reason: str = ""


ScoreFn = Callable[[FloatArray], float]
RegimeFn = Callable[[FloatArray], RegimePrediction]
SetupSource = Literal["strategy", "grid"]


class MarketReplay:
    """Bars, contracts and setups of one strategy over a dataset (cached)."""

    def __init__(self, cfg: BotConfig, dataset: HistoricalDataset, strategy_id: str, *,
                 setups: SetupSource = "strategy", grid_every_minutes: int = 15,
                 ml_score: ScoreFn | None = None, regime_fn: RegimeFn | None = None) -> None:
        if strategy_id not in cfg.strategies.strategies:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        self.cfg = cfg
        self.dataset = dataset
        self.strategy_id = strategy_id
        self.scfg = cfg.strategies.strategies[strategy_id]
        self.times = session_times(cfg)
        self.index = Bars.from_candles(dataset.candles.get(cfg.settings.instruments.nifty_index_key, ()))
        self.futures = futures_bars(dataset)
        self.options = OptionHistory(dataset)
        self.setup_source = setups
        self.regime_fn = regime_fn
        self._market: dict[int, dict[str, float]] = {}
        if setups == "strategy":
            events = strategy_events(cfg, dataset, strategy_id)
        else:
            w = self.scfg.entry_window
            events = grid_events(dataset.days(), strategy_id=strategy_id, every_minutes=grid_every_minutes,
                                 start=w.start, end=w.end)
        self.dropped: dict[str, int] = {}
        self.setups: dict[date, list[Setup]] = {}
        for ev in sorted(events, key=lambda e: (e.as_of, e.event_id)):
            if ev.option_type is None:
                continue
            s = self._resolve(ev.as_of, ev.option_type, ev.reason, ml_score)
            if isinstance(s, str):
                self.dropped[s] = self.dropped.get(s, 0) + 1
                continue
            self.setups.setdefault(s.as_of.astimezone(IST).date(), []).append(s)

    def _resolve(self, as_of: datetime, opt: OptionType, reason: str, ml_score: ScoreFn | None) -> Setup | str:
        day = as_of.astimezone(IST).date()
        spot = self.spot(as_of)
        if spot is None:
            return "no NIFTY bars before the decision"
        expiry = self.options.expiry_for(day)
        inst = self.options.select(spot, opt, self.scfg.strike_selection, expiry) if expiry else None
        if inst is None:
            return "no contract"
        ctx = self.options.context(inst, as_of)
        if ctx is None:
            return "no option bars before the decision"
        bad = liquidity_reasons(ctx, self.scfg)
        if bad:
            return bad[0]
        if self.entry_bar(inst.instrument_key, as_of) is None:
            return "no entry bar"
        conf = None
        if ml_score is not None:
            feats = compute_features(self.index, as_of, times=self.times, direction=opt, futures_bars=self.futures,
                                     option=ctx)
            try:
                v = float(ml_score(feature_vector(feats).reshape(1, -1)))
                conf = v if math.isfinite(v) and 0.0 <= v <= 1.0 else None
            except Exception:
                conf = None
        return Setup(as_of, opt, inst, conf, reason)

    def days_with_setups(self, days: Sequence[date] | None = None) -> list[date]:
        pool = self.dataset.days() if days is None else list(days)
        return [d for d in pool if self.setups.get(d)]

    # Point-in-time lookups ---------------------------------------------------
    def spot(self, as_of: datetime) -> float | None:
        i = int(np.searchsorted(self.index.end, int(ensure_ist(as_of).timestamp()), side="right")) - 1
        if i < 0 or self.index.day[i] != ensure_ist(as_of).astimezone(IST).date().toordinal():
            return None
        return float(self.index.close[i])

    def session_open(self, day: date) -> float | None:
        s = self.index.session(day)
        return float(s.open[0]) if len(s) else None

    def market(self, as_of: datetime) -> dict[str, float]:
        k = int(ensure_ist(as_of).timestamp())
        if k not in self._market:
            if len(self._market) > 50_000:
                self._market.clear()
            self._market[k] = compute_features(self.index, as_of, times=self.times, futures_bars=self.futures)
        return self._market[k]

    def regime(self, market: dict[str, float]) -> RegimePrediction:
        if self.regime_fn is not None:
            try:
                return self.regime_fn(feature_vector(market).reshape(1, -1))
            except Exception:
                log.exception("regime model failed; using the trailing rules")
        return RuleBasedRegime.from_features(market)

    def last_premium(self, key: str, as_of: datetime) -> float | None:
        b = self.options.bars(key)
        ts = int(ensure_ist(as_of).timestamp())
        i = int(np.searchsorted(b.end, ts, side="right")) - 1
        if i < 0 or b.day[i] != ensure_ist(as_of).astimezone(IST).date().toordinal():
            return None
        return float(b.close[i])

    def entry_bar(self, key: str, as_of: datetime, max_delay: timedelta = timedelta(minutes=1)) -> int | None:
        """Index of the first bar starting at/after ``as_of`` (within the delay, same day)."""
        b = self.options.bars(key)
        ts = int(ensure_ist(as_of).timestamp())
        i = int(np.searchsorted(b.start, ts, side="left"))
        if i >= len(b) or b.start[i] - ts > max_delay.total_seconds() or \
                b.day[i] != ensure_ist(as_of).astimezone(IST).date().toordinal():
            return None
        return i

    def option_context(self, inst: Instrument, as_of: datetime) -> OptionContext | None:
        return self.options.context(inst, as_of)


# --------------------------------------------------------------------------- #
# The environment
# --------------------------------------------------------------------------- #


@dataclass
class _Position:
    setup: Setup
    entry: float  # fill (incl. slippage)
    initial_stop: float
    stop: float
    target: float | None
    lot_units: int
    lots_initial: int
    lots_open: int
    entry_time: datetime
    peak: float
    entry_raw: float = 0.0  # bar open the entry filled at (before slippage)
    partials: int = 0
    exit_time: datetime | None = None
    gross: float = 0.0  # realised so far at unslipped prices (exit raw - entry raw)
    costs: float = 0.0
    slippage: float = 0.0
    exit_value: float = 0.0  # sum of exit fills x qty
    exited_units: int = 0
    stop_history: list[tuple[str, float]] = field(default_factory=list)
    actions: dict[str, int] = field(default_factory=dict)

    def view(self) -> PositionView:
        return PositionView(self.setup.option_type, self.entry, self.initial_stop, self.stop, self.target,
                            self.lot_units, self.lots_initial, self.lots_open, self.entry_time, self.peak,
                            self.partials)


class TradeManagementEnv(gym.Env[Float32Array, np.int64]):
    """One episode = one trading day of one strategy (see the module docstring)."""

    metadata: dict[str, Any] = {"render_modes": []}

    def __init__(self, replay: MarketReplay, days: Sequence[date], *, rewards: RewardWeights | None = None,
                 normalizer: ObservationNormalizer | None = None, sequential: bool = False) -> None:
        super().__init__()
        self.replay = replay
        self.cfg = replay.cfg
        self.rules: RLConfig = replay.scfg.rl
        self.envelope = RiskEnvelope.from_config(replay.cfg, replay.strategy_id)
        self.weights = rewards or RewardWeights()
        self.normalizer = normalizer
        self.sequential = sequential
        self._day_ptr = 0
        self.charges = ChargeCalculator(self.cfg.settings.costs)
        self.times = replay.times
        self.action_space = spaces.Discrete(len(Action))
        lim = normalizer.clip if normalizer is not None else np.inf
        self.observation_space = spaces.Box(-lim, lim, shape=(len(OBS_NAMES),), dtype=np.float32)
        # episode state
        first = days[0] if days else date(2000, 1, 3)  # placeholder; no days -> refused below
        self.day: DayState = DayState(first)
        self._queue: list[Setup] = []
        self._setup: Setup | None = None
        self._pos: _Position | None = None
        self._t: datetime = datetime.combine(first, self.times.open, tzinfo=IST)
        self._eq_peak = 0.0
        self._max_dd = 0.0
        self.trades: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.last_raw: dict[str, float] | None = None
        self.episode_reward: dict[str, float] = {}
        # days with at least one setup the envelope would offer on a fresh day
        self.days = [d for d in replay.days_with_setups(days) if self._offerable(d)]
        if not self.days:
            raise ValueError("no day in the range has a setup the risk envelope would allow")
        self.day = DayState(self.days[0])

    def _offerable(self, d: date) -> bool:
        self.day = DayState(d)
        self._queue = list(self.replay.setups.get(d, []))
        self._setup, self._pos = None, None
        self._t = datetime.combine(d, self.times.open, tzinfo=IST)
        ok = self._next_decision()
        self._setup, self._queue, self.events = None, [], []
        return ok

    # ------------------------------------------------------------------ #
    def spec_dict(self) -> dict[str, Any]:
        return environment_spec(self.cfg, self.replay.strategy_id, self.weights, self.replay.setup_source)

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None
              ) -> tuple[Float32Array, dict[str, Any]]:
        super().reset(seed=seed)
        if options and "day" in options:
            d = options["day"]
            if d not in self.days:
                raise ValueError(f"{d} is not one of this environment's days")
        elif self.sequential:
            d = self.days[self._day_ptr % len(self.days)]
            self._day_ptr += 1
        else:
            d = self.days[int(self.np_random.integers(len(self.days)))]
        self.day = DayState(d)
        self._queue = list(self.replay.setups.get(d, []))
        self._setup, self._pos = None, None
        self._t = datetime.combine(d, self.times.open, tzinfo=IST)
        self._eq_peak, self._max_dd = 0.0, 0.0
        self.trades, self.events = [], []
        self.episode_reward = {}
        if not self._next_decision():
            raise RuntimeError(f"{d}: no decision point (no valid setup)")  # days_with_setups prevents this
        return self._observe(), self._info()

    def action_masks(self) -> npt.NDArray[np.bool_]:
        return action_mask(self._pos is not None, self._setup is not None)

    # ------------------------------------------------------------------ #
    def step(self, action: Any) -> tuple[Float32Array, float, bool, bool, dict[str, Any]]:
        a = Action(int(action))
        out = StepOutcome()
        closed_before = len(self.trades)
        t0 = self._t
        if self._setup is not None:
            setup, self._setup = self._setup, None
            if a is Action.ENTER:
                self._open(setup, out)
            elif a in (Action.SKIP_SETUP, Action.HOLD):
                self.events.append({"t": setup.as_of.isoformat(), "event": "SKIP_SETUP", "reason": setup.reason})
            else:
                out.violations += 1
                self.events.append({"t": setup.as_of.isoformat(), "event": "INVALID", "action": a.name,
                                    "why": "no position to manage"})
        elif self._pos is not None:
            pos = self._pos
            pos.actions[a.name] = pos.actions.get(a.name, 0) + 1
            exit_lots = 0
            if a in SETUP_ACTIONS:
                out.violations += 1
                self.events.append({"t": t0.isoformat(), "event": "INVALID", "action": a.name,
                                    "why": "no setup is offered while a trade is open"})
            elif a in STOP_ACTIONS:
                price = self.replay.last_premium(pos.setup.instrument.instrument_key, t0)
                new = proposed_stop(a, pos.view(), price or 0.0, self.rules, pos.setup.instrument.tick_size)
                if new is not None:
                    assert new > pos.stop  # tighten-only (defence in depth)
                    pos.stop = new
                    pos.stop_history.append((t0.isoformat(), new))
            elif a is Action.TAKE_PARTIAL_PROFIT:
                price = self.replay.last_premium(pos.setup.instrument.instrument_key, t0)
                exit_lots = partial_lots(pos.view(), price or 0.0, self.rules) or 0
            elif a is Action.EXIT_FULL:
                exit_lots = pos.lots_open
            self._simulate(out, market_exit_lots=exit_lots, exit_full=a is Action.EXIT_FULL)
        else:  # pragma: no cover - reset() guarantees a decision point
            raise RuntimeError("step() called on a finished episode; call reset()")
        terminated = not self._next_decision()
        rb = compute_reward(out, self.weights, self.envelope.risk_per_trade)
        for k, v in rb.as_dict().items():
            self.episode_reward[k] = self.episode_reward.get(k, 0.0) + v
        info = self._info()
        info.update(reward_components=rb.as_dict(), violations=out.violations, net_pnl=out.net_pnl,
                    closed=self.trades[closed_before:])
        obs = self._observe() if not terminated else np.zeros(len(OBS_NAMES), dtype=np.float32)
        return obs, float(rb.total), terminated, False, info

    # ------------------------------------------------------------------ #
    # Decision points
    # ------------------------------------------------------------------ #
    def _next_decision(self) -> bool:
        if self._pos is not None:
            return True  # _simulate already moved self._t to the next decision time
        slip = self.cfg.settings.paper_broker.slippage_ticks
        while self._queue:
            s = self._queue.pop(0)
            if s.as_of < self._t:
                self.events.append({"t": s.as_of.isoformat(), "event": "SETUP_MISSED", "why": "while in a trade"})
                continue
            why = self.envelope.entry_block_reason(s.as_of, self.day, 0)
            if why is None:
                prem = self.replay.last_premium(s.instrument.instrument_key, s.as_of)
                est = self._levels(prem) if prem else None
                if est is None or self.envelope.lots(prem or 0.0, est[0], s.instrument.lot_size,
                                                     slip * s.instrument.tick_size,
                                                     self.envelope.remaining_budget(self.day)) < 1:
                    why = "position size zero"
            if why is not None:
                self.events.append({"t": s.as_of.isoformat(), "event": "SETUP_BLOCKED", "why": why})
                continue
            self._setup, self._t = s, s.as_of
            return True
        return False

    def _levels(self, entry: float, tick: float = 0.05) -> tuple[float, float | None] | None:
        sl, tp = self.replay.scfg.stop_loss, self.replay.scfg.take_profit
        raw = entry * (1 - sl.value / 100) if sl.mode == "PREMIUM_PERCENT" else entry - sl.value
        stop = _ceil_tick(raw, tick)
        if stop <= 0 or stop >= entry:
            return None
        r = entry - stop
        if tp.mode == "R_MULTIPLE":
            target = entry + tp.value * r
        elif tp.mode == "PREMIUM_PERCENT":
            target = entry * (1 + tp.value / 100)
        else:
            target = entry + tp.value
        return stop, _ceil_tick(target, tick)

    # ------------------------------------------------------------------ #
    # Fills
    # ------------------------------------------------------------------ #
    def _open(self, s: Setup, out: StepOutcome) -> None:
        inst = s.instrument
        bars = self.replay.options.bars(inst.instrument_key)
        i = self.replay.entry_bar(inst.instrument_key, s.as_of)
        why = self.envelope.entry_block_reason(s.as_of, self.day, 0)
        if i is None or why is not None:  # pragma: no cover - checked when the setup was offered
            self.events.append({"t": s.as_of.isoformat(), "event": "ENTRY_FAILED", "why": why or "no entry bar"})
            return
        slip = self.cfg.settings.paper_broker.slippage_ticks * inst.tick_size
        raw = float(bars.open[i])
        fill = round(raw + slip, 2)
        lv = self._levels(fill, inst.tick_size)
        if lv is None:
            self.events.append({"t": s.as_of.isoformat(), "event": "ENTRY_FAILED", "why": "invalid levels"})
            return
        stop, target = lv
        lots = self.envelope.lots(fill, stop, inst.lot_size, slip, self.envelope.remaining_budget(self.day))
        if lots < 1:
            self.events.append({"t": s.as_of.isoformat(), "event": "ENTRY_FAILED", "why": "position size zero"})
            return
        qty = lots * inst.lot_size
        fees = self.charges.compute(OrderSide.BUY, qty, fill).total
        out.costs += fees
        out.slippage += (fill - raw) * qty
        out.entries += 1
        self.day.trades += 1
        start = datetime.fromtimestamp(int(bars.start[i]), IST)
        self._pos = _Position(s, fill, stop, stop, target, inst.lot_size, lots, lots, start, fill, entry_raw=raw,
                              costs=fees, slippage=(fill - raw) * qty, stop_history=[(start.isoformat(), stop)])
        self._t = start  # the entry bar starts now; _simulate walks it
        self._simulate(out, market_exit_lots=0, exit_full=False)

    def _exit(self, lots: int, raw: float, at: datetime, reason: str, out: StepOutcome) -> None:
        pos = self._pos
        assert pos is not None
        lots = min(lots, pos.lots_open)
        if lots <= 0:
            return
        tick = pos.setup.instrument.tick_size
        slip = self.cfg.settings.paper_broker.slippage_ticks * tick
        fill = max(tick, round(raw - slip, 2))
        qty = lots * pos.lot_units
        fees = self.charges.compute(OrderSide.SELL, qty, fill).total
        gross = (raw - pos.entry_raw) * qty  # unslipped; slippage is charged separately
        pos.gross += gross
        pos.costs += fees
        pos.slippage += (raw - fill) * qty
        pos.exit_value += fill * qty
        pos.exited_units += qty
        pos.lots_open -= lots
        out.realized_pnl += gross
        out.costs += fees
        out.slippage += (raw - fill) * qty
        if pos.lots_open > 0:
            pos.partials += 1
            self.events.append({"t": at.isoformat(), "event": "PARTIAL_EXIT", "lots": lots, "price": fill})
            return
        # gross + slippage + costs reconcile exactly: net = (avg exit fill - entry fill) x qty - charges
        net = pos.gross - pos.costs - pos.slippage
        avg_exit = pos.exit_value / pos.exited_units
        fill_gross = pos.exit_value - pos.entry * pos.exited_units
        pos.exit_time = at
        r = pos.entry - pos.initial_stop
        r_mult = round((avg_exit - pos.entry) / r, 4) if r > 0 else None
        rec = {"strategy_id": self.replay.strategy_id, "day": self.day.day.isoformat(),
               "option_type": pos.setup.option_type.value, "instrument_key": pos.setup.instrument.instrument_key,
               "signal_time": pos.setup.as_of.isoformat(), "entry_time": pos.entry_time.isoformat(),
               "exit_time": at.isoformat(), "entry": pos.entry, "exit": round(avg_exit, 4),
               "initial_stop": pos.initial_stop, "final_stop": pos.stop, "target": pos.target,
               "lots": pos.lots_initial, "quantity": pos.lots_initial * pos.lot_units, "exit_reason": reason,
               "partials": pos.partials, "gross_pnl": round(fill_gross, 2), "charges": round(pos.costs, 2),
               "slippage": round(pos.slippage, 2), "net_pnl": round(net, 2),
               "r_multiple": r_mult,
               "holding_minutes": round(_minutes(pos.entry_time, at), 2), "stop_history": list(pos.stop_history),
               "actions": dict(pos.actions), "risk_budget": self.envelope.risk_per_trade,
               "ml_confidence": pos.setup.ml_confidence}
        self.trades.append(rec)
        d = self.day
        d.realized_net += net
        if net < 0:
            d.consecutive_losses += 1
            d.last_loss_at = at
        else:
            d.consecutive_losses = 0
        d.outcomes.append(PastOutcome(at, net, r_mult))
        if d.loss >= self.envelope.max_daily_loss - 1e-9 and not d.halted:
            d.halted = "daily loss limit reached"
        self._pos = None

    def _simulate(self, out: StepOutcome, *, market_exit_lots: int, exit_full: bool) -> None:
        """Walk bars from ``self._t`` to the next decision time (or the exit)."""
        pos = self._pos
        assert pos is not None
        key = pos.setup.instrument.instrument_key
        bars = self.replay.options.bars(key)
        day_ord = self.day.day.toordinal()
        sq = datetime.combine(self.day.day, self.envelope.square_off_start, tzinfo=IST)
        sq_ts = int(sq.timestamp())
        start_ts = int(self._t.timestamp())
        end = self._t + timedelta(minutes=self.rules.decision_interval_minutes)
        end_ts = int(end.timestamp())
        j = int(np.searchsorted(bars.start, start_ts, side="left"))
        first = True
        last_close: float | None = self.replay.last_premium(key, self._t)
        last_end = self._t
        hold_from = pos.entry_time + timedelta(minutes=self.weights.max_hold_minutes)
        while self._pos is not None:
            if j >= len(bars) or bars.day[j] != day_ord:
                # data ended for the day: close at the last known price
                self._exit(pos.lots_open, last_close if last_close else pos.entry, last_end, "DATA_END", out)
                break
            bstart = int(bars.start[j])
            if bstart >= end_ts and bstart < sq_ts:
                break  # next decision point
            o, h, lo, c = (float(bars.open[j]), float(bars.high[j]), float(bars.low[j]), float(bars.close[j]))
            at_open = datetime.fromtimestamp(bstart, IST)
            at_end = datetime.fromtimestamp(int(bars.end[j]), IST)
            if bstart >= sq_ts:
                self._exit(pos.lots_open, o, at_open, "SQUARE_OFF", out)
                break
            if first and market_exit_lots > 0:
                self._exit(market_exit_lots, o, at_open, "RL_EXIT" if exit_full else "RL_PARTIAL", out)
                if self._pos is None:
                    break
            first = False
            if o <= pos.stop:
                self._exit(pos.lots_open, o, at_open, self._stop_reason(pos), out)
                break
            if pos.target is not None and o >= pos.target:
                self._exit(pos.lots_open, o, at_open, "TARGET", out)
                break
            hit = False
            for p in ((lo, h) if c >= o else (h, lo)):
                if p <= pos.stop:
                    self._exit(pos.lots_open, pos.stop, at_end, self._stop_reason(pos), out)
                    hit = True
                    break
                if pos.target is not None and p >= pos.target:
                    self._exit(pos.lots_open, pos.target, at_end, "TARGET", out)
                    hit = True
                    break
                pos.peak = max(pos.peak, p)
            if hit:
                break
            pos.peak = max(pos.peak, c)
            last_close, last_end = c, at_end
            # holding penalty for minutes beyond the max hold
            if at_end > hold_from:
                out.minutes_over_hold += _minutes(max(at_open, hold_from), at_end)
            # mark to market: drawdown and the daily loss guard (envelope)
            unreal = (c - pos.entry) * pos.lots_open * pos.lot_units
            equity = self.day.realized_net + (pos.gross - pos.costs - pos.slippage) + unreal
            self._mark(equity, out)
            if self.day.realized_net + (pos.gross - pos.costs - pos.slippage) + unreal <= \
                    -self.envelope.max_daily_loss:
                out.violations += 1
                self._exit(pos.lots_open, c, at_end, "DAILY_LOSS_LIMIT", out)
                self.day.halted = "daily loss limit reached"
                break
            j += 1
        if self._pos is None:
            self._mark(self.day.realized_net, out)
            self._t = max(pos.exit_time or last_end, self._t)
        else:
            self._t = max(end, self._t)

    @staticmethod
    def _stop_reason(pos: _Position) -> str:
        return "TRAILING_STOP" if pos.stop > pos.initial_stop + 1e-9 else "STOP_LOSS"

    def _mark(self, equity: float, out: StepOutcome) -> None:
        self._eq_peak = max(self._eq_peak, equity)
        dd = self._eq_peak - equity
        if dd > self._max_dd:
            out.drawdown_increase += dd - self._max_dd
            self._max_dd = dd

    # ------------------------------------------------------------------ #
    # Observations
    # ------------------------------------------------------------------ #
    def account_view(self) -> AccountView:
        e = self.envelope
        return AccountView(open_positions=1 if self._pos is not None else 0, realized_pnl_today=self.day.realized_net,
                           max_daily_loss=e.max_daily_loss, trades_today=self.day.trades,
                           max_trades_per_day=e.max_trades_per_day,
                           cooldown_active=e.cooldown_active(self._t, self.day),
                           consecutive_losses=self.day.consecutive_losses, risk_budget=e.risk_per_trade)

    def raw_observation(self) -> dict[str, float]:
        t = self._t
        market = self.replay.market(t)
        if self._pos is not None:
            inst, setup_view, pos_view = self._pos.setup.instrument, None, self._pos.view()
        elif self._setup is not None:
            inst, pos_view = self._setup.instrument, None
            setup_view = SetupView(self._setup.option_type, self._setup.ml_confidence)
        else:
            inst, setup_view, pos_view = None, None, None
        ctx = self.replay.option_context(inst, t) if inst is not None else None
        premium = ctx.premium if ctx is not None else None
        return build_observation(market=market, spot=self.replay.spot(t),
                                 session_open_price=self.replay.session_open(self.day.day), as_of=t, times=self.times,
                                 no_new_entries_after=self.envelope.no_new_entries_after, option=ctx, premium=premium,
                                 position=pos_view, setup=setup_view, account=self.account_view(),
                                 outcomes=self.day.outcomes, regime=self.replay.regime(market))

    def _observe(self) -> Float32Array:
        raw = self.raw_observation()
        self.last_raw = raw
        x = observation_vector(raw)
        if self.normalizer is None:  # un-normalised (diagnostics): NaN -> 0
            return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        return self.normalizer.transform(x)

    def _info(self) -> dict[str, Any]:
        return {"day": self.day.day.isoformat(), "t": self._t.isoformat(), "in_position": self._pos is not None,
                "setup_offered": self._setup is not None, "action_mask": self.action_masks(),
                "realized_net": round(self.day.realized_net, 2), "trades_today": self.day.trades}


def environment_spec(cfg: BotConfig, strategy_id: str, weights: RewardWeights, setups: str = "strategy",
                     ) -> dict[str, Any]:
    """Everything that defines the environment (saved with every model)."""
    scfg = cfg.strategies.strategies[strategy_id]
    return {
        "version": RL_ENV_VERSION, "strategy_id": strategy_id, "episode": "one trading day of one strategy",
        "setups": setups, "observation_schema": obs_schema(),
        "actions": {a.name: a.value for a in Action},
        "action_rules": scfg.rl.model_dump(mode="json"),
        "decision_points": {"setup": "valid setup the envelope allows (ENTER / SKIP_SETUP)",
                            "position": f"every {scfg.rl.decision_interval_minutes} min while a trade is open"},
        "execution": {"fill": "open of the option bar starting at the decision time",
                      "slippage_ticks": cfg.settings.paper_broker.slippage_ticks,
                      "intrabar_path": "up-bar low first, down-bar high first; gaps fill at the open",
                      "costs": "broker.charges.ChargeCalculator (brokerage, STT, exchange, SEBI, stamp, GST)"},
        "risk_envelope": RiskEnvelope.from_config(cfg, strategy_id).as_dict(),
        "initial_levels": {"stop_loss": scfg.stop_loss.model_dump(mode="json"),
                           "take_profit": scfg.take_profit.model_dump(mode="json")},
        "agent_never_controls": ["daily loss limits", "maximum exposure / open trades", "kill switch",
                                 "credentials", "square-off timing", "position size (sized by the envelope)"],
        "reward": reward_spec(weights),
        "market_feature_schema": FEATURE_SCHEMA_VERSION,
    }


__all__ = ["OBS_GROUPS", "OBS_NAMES", "OBS_SCHEMA_VERSION", "POSITION_ACTIONS", "RL_ENV_VERSION", "SETUP_ACTIONS",
           "STOP_ACTIONS", "AccountView", "Action", "DayState", "MarketReplay", "NormalizerError",
           "ObservationNormalizer", "PositionView", "RiskEnvelope", "Setup", "SetupView", "TradeManagementEnv",
           "action_mask", "build_observation", "environment_spec", "obs_schema", "observation_vector",
           "partial_lots", "proposed_stop"]
