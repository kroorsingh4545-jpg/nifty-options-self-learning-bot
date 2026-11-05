"""RL trade-management environment (Phase 8), on SYNTHETIC data only.

Covers the action space boundary (what the agent can never control), the
bounded action rules (stops only tighten), the risk envelope, point-in-time
observations, the fill/P&L accounting and the Gymnasium API contract.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import pytest
from gymnasium.utils.env_checker import check_env

from backtest.data import HistoricalDataset
from config.settings import RLConfig
from core.clock import IST
from core.enums import OptionType
from core.models import Candle
from ml.reward_design import RewardWeights
from ml.rl_environment import (
    OBS_NAMES,
    Action,
    DayState,
    MarketReplay,
    NormalizerError,
    ObservationNormalizer,
    PositionView,
    RiskEnvelope,
    TradeManagementEnv,
    action_mask,
    environment_spec,
    obs_schema,
    observation_vector,
    partial_lots,
    proposed_stop,
)
from tests.ml_support import config, synthetic_dataset

SID = "vwap_ema"
NO_SHAPING = RewardWeights(drawdown=0, holding_per_minute=0, overtrading_per_entry=0, risk_violation=1.0)


@lru_cache(maxsize=2)
def replay(days: int = 12) -> MarketReplay:
    return MarketReplay(config(), synthetic_dataset(days), SID, setups="grid", grid_every_minutes=15)


def make_env(**kw: Any) -> TradeManagementEnv:
    r = replay()
    return TradeManagementEnv(r, r.dataset.days(), **kw)


def run_episode(env: TradeManagementEnv, policy: Any, day: date | None = None) -> tuple[float, list[dict[str, Any]]]:
    obs, info = env.reset(options={"day": day} if day else None)
    total, done, infos = 0.0, False, [info]
    while not done:
        obs, r, done, trunc, info = env.step(policy(env, info))
        assert not trunc
        total += r
        infos.append(info)
    return total, infos


def random_policy(seed: int) -> Any:
    rng = random.Random(seed)  # noqa: S311

    def pick(env: TradeManagementEnv, info: dict[str, Any]) -> int:
        return rng.randrange(len(Action))
    return pick


def valid_random_policy(seed: int) -> Any:
    rng = random.Random(seed)  # noqa: S311

    def pick(env: TradeManagementEnv, info: dict[str, Any]) -> int:
        return rng.choice([i for i, ok in enumerate(info["action_mask"]) if ok])
    return pick


# --------------------------------------------------------------------------- #
# Action boundary
# --------------------------------------------------------------------------- #


def test_action_space_is_exactly_the_trade_management_actions() -> None:
    assert [a.name for a in Action] == ["HOLD", "TIGHTEN_STOP", "MOVE_STOP_TO_BREAKEVEN", "TRAIL_STOP",
                                        "TAKE_PARTIAL_PROFIT", "EXIT_FULL", "ENTER", "SKIP_SETUP"]
    forbidden = ("LOSS", "LIMIT", "EXPOSURE", "KILL", "CREDENTIAL", "TOKEN", "SQUARE", "SIZE", "WIDEN", "LIVE")
    for a in Action:
        assert not any(f in a.name for f in forbidden), a
    env = make_env()
    assert env.action_space.n == len(Action)


def test_rl_config_has_no_risk_limit_fields() -> None:
    fields = set(RLConfig.model_fields)
    assert fields == {"enabled", "tighten_fraction", "trail_distance_r", "partial_fraction", "max_partial_exits",
                      "min_profit_r_for_partial", "decision_interval_minutes"}
    assert RLConfig().enabled is False  # off by default
    for sid, s in config().strategies.strategies.items():
        assert s.rl.enabled is False, sid


def test_action_masks() -> None:
    assert action_mask(True, False).tolist() == [True] * 6 + [False, False]
    assert action_mask(False, True).tolist() == [False] * 6 + [True, True]
    assert action_mask(False, False).tolist() == [True] + [False] * 7


# --------------------------------------------------------------------------- #
# Action rules: stops only tighten, never at/above the market
# --------------------------------------------------------------------------- #


def _pos(entry: float = 100.0, stop: float = 75.0, lots: int = 4, peak: float = 100.0, partials: int = 0
         ) -> PositionView:
    return PositionView(OptionType.CE, entry, 75.0, stop, 150.0, 65, 4, lots,
                        datetime(2026, 9, 1, 10, 0, tzinfo=IST), peak, partials)


@pytest.mark.parametrize("action", [Action.TIGHTEN_STOP, Action.MOVE_STOP_TO_BREAKEVEN, Action.TRAIL_STOP])
def test_stop_actions_only_ever_tighten(action: Action) -> None:
    rules = RLConfig()
    for stop in np.arange(75.0, 140.0, 2.5):
        for price in np.arange(60.0, 200.0, 0.35):
            p = _pos(stop=float(stop))
            new = proposed_stop(action, p, float(price), rules, 0.05)
            if new is None:
                continue
            assert new > p.stop  # strictly tighter
            assert new <= price - 0.05 + 1e-9  # strictly below the market
            assert abs(round(new / 0.05) * 0.05 - new) < 1e-6  # on the tick


def test_stop_action_values() -> None:
    rules = RLConfig(tighten_fraction=0.25, trail_distance_r=0.5)
    p = _pos()
    assert proposed_stop(Action.TIGHTEN_STOP, p, 135.0, rules, 0.05) == 90.0  # 75 + 0.25 * 60
    assert proposed_stop(Action.MOVE_STOP_TO_BREAKEVEN, p, 120.0, rules, 0.05) == 100.0
    assert proposed_stop(Action.MOVE_STOP_TO_BREAKEVEN, p, 100.02, rules, 0.05) is None  # not above entry yet
    assert proposed_stop(Action.TRAIL_STOP, p, 140.0, rules, 0.05) == 127.5  # 140 - 0.5 * 25
    assert proposed_stop(Action.TRAIL_STOP, p, 80.0, rules, 0.05) is None  # would loosen
    assert proposed_stop(Action.HOLD, p, 140.0, rules, 0.05) is None
    assert proposed_stop(Action.EXIT_FULL, p, 140.0, rules, 0.05) is None


def test_partial_profit_rules() -> None:
    rules = RLConfig(partial_fraction=0.5, max_partial_exits=1, min_profit_r_for_partial=0.5)
    assert partial_lots(_pos(), 120.0, rules) == 2
    assert partial_lots(_pos(), 110.0, rules) is None  # +0.4R: not enough profit
    assert partial_lots(_pos(lots=1), 140.0, rules) is None  # at least one lot always stays
    assert partial_lots(_pos(lots=3), 140.0, rules) == 1
    assert partial_lots(_pos(partials=1), 140.0, rules) is None  # at most max_partial_exits
    assert partial_lots(_pos(lots=2), 140.0, RLConfig(partial_fraction=0.9)) == 1


# --------------------------------------------------------------------------- #
# Risk envelope (from config; the agent cannot change it)
# --------------------------------------------------------------------------- #


def test_envelope_comes_from_the_risk_config() -> None:
    cfg = config()
    e = RiskEnvelope.from_config(cfg, SID)
    s = cfg.strategies.strategies[SID]
    assert e.risk_per_trade == min(cfg.risk.per_trade.max_risk_per_trade, s.sizing.risk_per_trade) <= 2000
    assert e.max_daily_loss <= 5000
    assert e.max_trades_per_day <= 4
    assert e.loss_cooldown_minutes >= 15
    assert e.max_consecutive_losses <= 3
    assert e.no_new_entries_after == time(14, 45)
    assert e.square_off_start == time(15, 10)


def test_envelope_blocks_entries() -> None:
    e = RiskEnvelope.from_config(config(), SID)
    d = date(2026, 9, 1)
    at = datetime.combine(d, time(10, 30), tzinfo=IST)
    assert e.entry_block_reason(at, DayState(d), 0) is None
    assert e.entry_block_reason(datetime.combine(d, time(14, 45), tzinfo=IST), DayState(d), 0)  # window / 14:45
    assert e.entry_block_reason(datetime.combine(d, time(9, 20), tzinfo=IST), DayState(d), 0)  # before the window
    assert e.entry_block_reason(datetime.combine(d, time(15, 12), tzinfo=IST), DayState(d), 0)
    assert e.entry_block_reason(at, DayState(d), 1) == "a trade is already open"
    assert e.entry_block_reason(at, DayState(d, trades=e.max_trades_per_day), 0) == "daily trade cap reached"
    assert e.entry_block_reason(at, DayState(d, consecutive_losses=e.max_consecutive_losses), 0) == \
        "consecutive-loss pause"
    assert e.entry_block_reason(at, DayState(d, last_loss_at=at - timedelta(minutes=5)), 0) == "loss cooldown"
    assert e.entry_block_reason(at, DayState(d, halted="daily loss limit reached"), 0)
    assert e.entry_block_reason(at, DayState(d, realized_net=-e.max_daily_loss), 0)
    # remaining budget shrinks with the day's loss (as risk.DailyLossGuard)
    assert e.remaining_budget(DayState(d, realized_net=-(e.max_daily_loss - 500))) == 500


def test_envelope_sizing() -> None:
    e = RiskEnvelope.from_config(config(), SID)
    assert e.lots(100.0, 75.0, 65, 0.05, 2000.0) == 1  # 2000 / (25.05 * 65) = 1.2
    assert e.lots(100.0, 90.0, 65, 0.05, 2000.0) == 3
    assert e.lots(100.0, 75.0, 65, 0.05, 1000.0) == 0
    assert e.lots(100.0, 100.0, 65, 0.0, 2000.0) == 0


# --------------------------------------------------------------------------- #
# Environment behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.filterwarnings("ignore::UserWarning")  # un-normalised env: unbounded Box
def test_gymnasium_api_contract() -> None:
    env = make_env()
    check_env(env, skip_render_check=True)
    obs, info = env.reset(seed=3)
    assert obs.shape == (len(OBS_NAMES),) and obs.dtype == np.float32
    assert info["setup_offered"] and not info["in_position"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_risk_limits_hold_under_any_policy(seed: int) -> None:
    """Even a uniformly random agent (including invalid actions) can never
    breach the envelope: entries before 14:45, flat by 15:10, stops never
    loosen, the daily trade cap, sizing within the risk budget."""
    env = make_env()
    e = env.envelope
    pol = random_policy(seed)
    for day in env.days:
        run_episode(env, pol, day)
        assert len(env.trades) <= e.max_trades_per_day
        for t in env.trades:
            entry, exit_ = datetime.fromisoformat(t["entry_time"]), datetime.fromisoformat(t["exit_time"])
            assert entry.time() < e.no_new_entries_after
            assert entry.time() >= e.entry_start
            assert exit_.time() <= e.square_off_start
            stops = [s for _, s in t["stop_history"]]
            assert stops == sorted(stops) and stops[0] == t["initial_stop"] < t["entry"]
            assert t["final_stop"] == stops[-1]
            per_unit = t["entry"] - t["initial_stop"] + 0.05
            assert per_unit * t["quantity"] <= e.risk_per_trade + 1e-6
            assert t["exit_reason"] in {"STOP_LOSS", "TRAILING_STOP", "TARGET", "SQUARE_OFF", "RL_EXIT",
                                        "DAILY_LOSS_LIMIT", "DATA_END"}
        # entries never follow a loss inside the cooldown
        for a, b in zip(env.trades, env.trades[1:], strict=False):
            if a["net_pnl"] < 0:
                gap = datetime.fromisoformat(b["signal_time"]) - datetime.fromisoformat(a["exit_time"])
                assert gap >= timedelta(minutes=e.loss_cooldown_minutes)


def test_unshaped_episode_return_equals_net_pnl() -> None:
    env = make_env(rewards=NO_SHAPING)
    pol = valid_random_policy(5)  # valid actions only -> no violation penalties
    for day in env.days[:6]:
        total, infos = run_episode(env, pol, day)
        net = sum(t["net_pnl"] for t in env.trades)
        assert total == pytest.approx(net / env.envelope.risk_per_trade, abs=1e-6)
        for t in env.trades:  # gross - charges = net, gross from fills
            assert t["gross_pnl"] - t["charges"] == pytest.approx(t["net_pnl"], abs=0.02)
            assert t["gross_pnl"] == pytest.approx((t["exit"] - t["entry"]) * t["quantity"], abs=0.05)


def test_enter_fills_at_the_next_bar_open_plus_slippage() -> None:
    env = make_env()
    obs, info = env.reset(options={"day": env.days[0]})
    setup = env._setup
    assert setup is not None
    env.step(Action.ENTER)
    t = env.trades[0] if env.trades else None
    bars = env.replay.options.bars(setup.instrument.instrument_key)
    i = env.replay.entry_bar(setup.instrument.instrument_key, setup.as_of)
    assert i is not None
    slip = env.cfg.settings.paper_broker.slippage_ticks * setup.instrument.tick_size
    entry = (t["entry"] if t else env._pos.entry)  # type: ignore[union-attr]
    assert entry == pytest.approx(round(float(bars.open[i]) + slip, 2))
    assert datetime.fromtimestamp(int(bars.start[i]), IST) >= setup.as_of


def test_exit_full_closes_at_the_next_bar_open() -> None:
    env = make_env()
    for day in env.days:
        env.reset(options={"day": day})
        env.step(Action.ENTER)
        if env._pos is None:
            continue
        t = env._t
        key = env._pos.setup.instrument.instrument_key
        _, _, _, _, info = env.step(Action.EXIT_FULL)
        rec = info["closed"][0] if info["closed"] else env.trades[-1]
        bars = env.replay.options.bars(key)
        j = int(np.searchsorted(bars.start, int(t.timestamp())))
        if rec["exit_reason"] != "RL_EXIT":
            continue  # a stop/target on the same bar's open came first
        slip = env.cfg.settings.paper_broker.slippage_ticks * 0.05
        assert rec["exit"] == pytest.approx(round(float(bars.open[j]) - slip, 2))
        assert datetime.fromisoformat(rec["exit_time"]) == datetime.fromtimestamp(int(bars.start[j]), IST)
        return
    pytest.fail("no trade could be opened on any day")


def test_invalid_actions_are_refused_and_penalised() -> None:
    env = make_env()
    env.reset(options={"day": env.days[0]})
    _, r, _, _, info = env.step(Action.TIGHTEN_STOP)  # a stop action with no position
    assert info["violations"] == 1 and r <= -env.weights.risk_violation
    assert not env.trades and env._pos is None
    for day in env.days:
        env.reset(options={"day": day})
        env.step(Action.ENTER)
        if env._pos is not None:
            _, r, _, _, info = env.step(Action.ENTER)  # ENTER while a trade is open
            assert info["violations"] == 1
            assert env.day.trades == 1  # nothing was added
            return
    pytest.fail("no trade could be opened")


def test_the_agent_cannot_hold_past_square_off() -> None:
    env = make_env()
    for day in env.days:
        _, infos = run_episode(env, lambda e, i: Action.ENTER if i["setup_offered"] else Action.HOLD, day)
        for t in env.trades:
            assert datetime.fromisoformat(t["exit_time"]).time() <= env.envelope.square_off_start
    reasons = {t["exit_reason"] for t in env.trades}
    assert reasons <= {"STOP_LOSS", "TARGET", "SQUARE_OFF", "DATA_END", "DAILY_LOSS_LIMIT"}


def test_observations_are_finite_and_normalised() -> None:
    env = make_env()
    rows = []
    pol = valid_random_policy(11)
    for day in env.days[:4]:
        _, info = env.reset(options={"day": day})
        rows.append(observation_vector(env.raw_observation()))
        done = False
        while not done:
            _, _, done, _, info = env.step(pol(env, info))
            if not done:
                rows.append(observation_vector(env.raw_observation()))
    norm = ObservationNormalizer.fit(np.array(rows))
    env2 = make_env(normalizer=norm)
    obs, _ = env2.reset(seed=1)
    assert np.all(np.isfinite(obs)) and np.all(np.abs(obs) <= norm.clip)
    assert env2.observation_space.contains(obs)
    restored = ObservationNormalizer.from_dict(norm.to_dict())
    np.testing.assert_array_equal(restored.mean, norm.mean)
    bad = {**norm.to_dict(), "schema_hash": "x"}
    with pytest.raises(NormalizerError):
        ObservationNormalizer.from_dict(bad)


def _scramble_after(ds: HistoricalDataset, cutoff: datetime) -> HistoricalDataset:
    rng = random.Random(42)  # noqa: S311

    def one(c: Candle) -> Candle:
        if c.end <= cutoff:
            return c
        k = 1 + rng.uniform(-0.3, 0.3)
        o, cl = c.open * k, c.close * (2 - k)
        return c.model_copy(update={"open": o, "close": cl, "high": max(o, cl) * 1.2, "low": min(o, cl) * 0.8})
    return HistoricalDataset(ds.instruments, {k: tuple(one(c) for c in v) for k, v in ds.candles.items()},
                             source=ds.source, description=ds.description)


@pytest.mark.parametrize("cut", [(10, 40), (12, 5), (14, 20)])
def test_observations_never_use_future_bars(cut: tuple[int, int]) -> None:
    """Rewrite every bar (index, futures, options) ending after ``cutoff`` and
    replay the same actions: every observation up to the cutoff is identical."""
    ds = synthetic_dataset(12)
    day = ds.days()[3]
    cutoff = datetime.combine(day, time(*cut), tzinfo=IST)
    cfg = config()
    a = MarketReplay(cfg, ds, SID, setups="grid", grid_every_minutes=15)
    b = MarketReplay(cfg, _scramble_after(ds, cutoff), SID, setups="grid", grid_every_minutes=15)

    def trace(r: MarketReplay) -> list[tuple[str, list[float]]]:
        env = TradeManagementEnv(r, [day])
        env.reset(options={"day": day})
        out, done, n = [], False, 0
        while not done and env._t <= cutoff:
            out.append((env._t.isoformat(), observation_vector(env.raw_observation()).tolist()))
            act = Action.ENTER if env._setup is not None else (Action.TRAIL_STOP if n % 3 else Action.HOLD)
            _, _, done, _, _ = env.step(act)
            n += 1
        return out

    ta, tb = trace(a), trace(b)
    assert ta, "no decision before the cutoff"
    n = min(len(ta), len(tb))
    assert n >= 1
    for (t1, x1), (t2, x2) in zip(ta[:n], tb[:n], strict=True):
        assert t1 == t2
        np.testing.assert_array_equal(np.array(x1), np.array(x2))


def test_obs_schema_and_spec() -> None:
    s = obs_schema()
    assert s["names"] == list(OBS_NAMES) and len(set(OBS_NAMES)) == len(OBS_NAMES)
    assert len(s["hash"]) == 64
    for required in ("nifty_vs_open_pct", "vwap_dist_pct", "ema20_dist_pct", "atr14_pct", "rv_15m", "option_iv",
                     "delta", "theta_pct", "entry_premium_pct_spot", "unrealized_pnl_budget", "r_multiple",
                     "time_in_trade_min", "minutes_to_square_off", "p_trend_up", "ml_confidence", "open_positions",
                     "daily_pnl_frac", "recent_win_rate_5"):
        assert required in OBS_NAMES, required
    spec = environment_spec(config(), SID, RewardWeights())
    assert "square-off timing" in spec["agent_never_controls"]
    assert spec["risk_envelope"]["no_new_entries_after"] == "14:45"


@pytest.mark.parametrize("call", [True, False])
def test_black_scholes_greeks_match_finite_differences(call: bool) -> None:
    from options.pricing import black_scholes, bs_greeks
    s, k, y, v, r = 25_000.0, 25_100.0, 5 / 365, 0.13, 0.065
    g = bs_greeks(s, k, y, v, r, call)
    assert g is not None
    h = 1.0
    delta = (black_scholes(s + h, k, y, v, r, call) - black_scholes(s - h, k, y, v, r, call)) / (2 * h)
    gamma = (black_scholes(s + h, k, y, v, r, call) - 2 * black_scholes(s, k, y, v, r, call)
             + black_scholes(s - h, k, y, v, r, call)) / h**2
    vega = (black_scholes(s, k, y, v + 0.0005, r, call) - black_scholes(s, k, y, v - 0.0005, r, call)) / 0.1
    dt = 1e-5  # years
    theta = (black_scholes(s, k, y - dt, v, r, call) - black_scholes(s, k, y + dt, v, r, call)) / (2 * dt) / 365
    assert g["delta"] == pytest.approx(delta, rel=1e-3)
    assert g["gamma"] == pytest.approx(gamma, rel=1e-2)
    assert g["vega"] == pytest.approx(vega, rel=1e-3)
    assert g["theta"] == pytest.approx(theta, rel=1e-3)
    assert bs_greeks(s, k, 0.0, v, r, call) is None
