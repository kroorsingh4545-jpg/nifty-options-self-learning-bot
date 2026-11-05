"""PPO trade manager: training, walk-forward evaluation, versioned artefacts.

::

    HistoricalDataset (SYNTHETIC | HISTORICAL) -> MarketReplay (setups, contracts, bars)
        │ days with a setup the risk envelope would allow, chronological
        │ last ``holdout_fraction`` of days = final holdout (untouched), after an embargo
        ▼
    development days ── expanding walk-forward: blocks B0..Bn
        fold k: train on B0..B(k-1); embargo ``embargo_days``; test on Bk
        per seed (>= 5): fit the observation normaliser on TRAIN days only,
        train PPO (Stable-Baselines3), evaluate deterministically on Bk
        ▼
    final: normaliser + PPO per seed on all development days
        -> holdout: every seed, the seed ensemble, the static-stop baseline,
           SKIP-everything, and the current production RL model (if any)
        ▼
    gates (equity, trade quality, drawdown, risk compliance; never reward alone)
        ▼
    ModelRegistry.register(kind="rl_trade_manager", ModelType.RL_PPO)
        SYNTHETIC : TRAINED or REJECTED, research registry only, never promotable
        HISTORICAL: TRAINED -> VALIDATED when every gate passes -> named human -> PAPER_READY

Days are independent episodes (every position is flat by 15:10), so the only
information that crosses a day boundary is the previous close (gap feature);
the embargo between train and test blocks covers that.

The deployable model is the SEED ENSEMBLE: the mean action probabilities of
the per-seed policies, restricted to the actions valid in the current state;
ties resolve to the safest action (HOLD, then SKIP_SETUP). Policies are saved
as plain tensors (``torch.save(state_dict)``) and loaded with
``weights_only=True``: no pickled code is ever loaded.
"""

from __future__ import annotations

import json
import math
import platform
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import gymnasium
import numpy as np
import numpy.typing as npt
import stable_baselines3
import torch
from gymnasium import spaces
from stable_baselines3 import PPO
from stable_baselines3.common.distributions import CategoricalDistribution
from stable_baselines3.common.policies import ActorCriticPolicy

from backtest.data import HistoricalDataset
from config.settings import BotConfig
from core.clock import IST
from core.enums import ModelRunStatus, ModelType
from ml.lgbm import DEFAULT_SEEDS, MIN_SEEDS, sha256_file
from ml.model_registry import LoadedModel, ModelRegistry
from ml.reward_design import REWARD_VERSION, RewardWeights
from ml.rl_environment import (
    OBS_NAMES,
    OBS_SCHEMA_VERSION,
    RL_ENV_VERSION,
    Action,
    MarketReplay,
    ObservationNormalizer,
    RiskEnvelope,
    SetupSource,
    TradeManagementEnv,
    environment_spec,
    obs_schema,
    observation_vector,
)

MODEL_KIND = "rl_trade_manager"
PIPELINE_VERSION = "1.0.0"
SAFE_ORDER = (Action.HOLD, Action.SKIP_SETUP)  # tie-break preference
SYNTHETIC_BANNER = ("SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real "
                    "NIFTY prices. It can never be validated, promoted or used for trading.")
Float32Array = npt.NDArray[np.float32]
BoolArray = npt.NDArray[np.bool_]
Policy = Callable[[Float32Array, BoolArray], int]


class RLTrainingError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PPOParams:
    learning_rate: float = 3e-4
    n_steps: int = 512
    batch_size: int = 64
    n_epochs: int = 10
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_range: float = 0.2
    ent_coef: float = 0.01
    vf_coef: float = 0.5
    max_grad_norm: float = 0.5
    net_arch: tuple[int, ...] = (64, 64)

    def sb3_kwargs(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("net_arch")
        return d

    def policy_kwargs(self) -> dict[str, Any]:
        return {"net_arch": {"pi": list(self.net_arch), "vf": list(self.net_arch)}}


@dataclass(frozen=True)
class RLGates:
    min_holdout_trades: int = 20
    require_positive_expectancy: bool = True  # ensemble, holdout, net of all costs
    require_beats_baseline: bool = True  # ensemble holdout net P&L > static-stop baseline
    min_positive_seed_fraction: float = 0.6  # seeds with positive walk-forward OOS net P&L
    max_drawdown_daily_loss_multiple: float = 2.0  # holdout max drawdown <= this x daily loss limit
    max_invalid_action_rate: float = 0.02  # refused actions / decisions (holdout)


@dataclass(frozen=True)
class RLTrainingConfig:
    seeds: tuple[int, ...] = DEFAULT_SEEDS
    total_timesteps: int = 30_000
    ppo: PPOParams = PPOParams()
    rewards: RewardWeights = RewardWeights()
    n_folds: int = 2
    embargo_days: int = 1
    holdout_fraction: float = 0.2
    min_holdout_days: int = 1
    setups: SetupSource = "grid"
    grid_every_minutes: int = 15
    normalizer_random_episodes: int = 2  # per train day, plus one static-policy pass
    gates: RLGates = RLGates()

    def __post_init__(self) -> None:
        if len(set(self.seeds)) < MIN_SEEDS:
            raise RLTrainingError(f"at least {MIN_SEEDS} distinct seeds are required")
        if self.total_timesteps < self.ppo.n_steps:
            raise RLTrainingError("total_timesteps must be >= ppo.n_steps")
        if self.n_folds < 1 or self.embargo_days < 0 or not 0 < self.holdout_fraction < 0.5:
            raise RLTrainingError("need n_folds >= 1, embargo_days >= 0 and 0 < holdout_fraction < 0.5")

    def hyperparameters(self) -> dict[str, Any]:
        d = asdict(self)
        d["seeds"] = list(self.seeds)
        return d


# --------------------------------------------------------------------------- #
# The model (seed ensemble of PPO policies + frozen normaliser)
# --------------------------------------------------------------------------- #


def _spaces(clip: float) -> tuple[spaces.Box, spaces.Discrete]:
    return (spaces.Box(-clip, clip, shape=(len(OBS_NAMES),), dtype=np.float32), spaces.Discrete(len(Action)))


def _action_probs(policy: ActorCriticPolicy, xt: torch.Tensor) -> npt.NDArray[np.float64]:
    dist = policy.get_distribution(xt)
    if not isinstance(dist, CategoricalDistribution):
        raise RLTrainingError("the policy must have a categorical (discrete) action distribution")
    return np.asarray(dist.distribution.probs.cpu().numpy(), dtype=np.float64)


def masked_choice(probs: npt.NDArray[np.float64], mask: BoolArray) -> int:
    """Highest-probability VALID action; ties go to HOLD, then SKIP_SETUP."""
    if mask.shape != (len(Action),) or not mask.any():
        raise ValueError("the action mask must allow at least one action")
    p = np.where(mask, probs, -1.0)
    best = float(p.max())
    for a in SAFE_ORDER:
        if mask[a.value] and p[a.value] >= best - 1e-12:
            return a.value
    return int(np.argmax(p))


class PPOTradeManager:
    """Deployable RL trade manager: per-seed PPO policies, averaged."""

    kind = MODEL_KIND

    def __init__(self, policies: Sequence[ActorCriticPolicy], seeds: Sequence[int], normalizer: ObservationNormalizer,
                 ppo: PPOParams) -> None:
        if len(policies) != len(seeds) or len(set(seeds)) < MIN_SEEDS:
            raise RLTrainingError(f"need one policy per seed and >= {MIN_SEEDS} seeds")
        self.policies = list(policies)
        self.seeds = tuple(int(s) for s in seeds)
        self.normalizer = normalizer
        self.ppo = ppo
        for p in self.policies:
            p.set_training_mode(False)

    # Inference -----------------------------------------------------------
    def probabilities(self, x: Float32Array) -> npt.NDArray[np.float64]:
        """Per-seed action probabilities for normalised observations, shape (seeds, n, actions)."""
        xt = torch.as_tensor(np.atleast_2d(x), dtype=torch.float32)
        with torch.no_grad():
            out = [_action_probs(p, xt) for p in self.policies]
        return np.asarray(out, dtype=np.float64)

    def act_normalized(self, x: Float32Array, mask: BoolArray) -> tuple[int, dict[str, Any]]:
        per_seed = self.probabilities(x)[:, 0, :]
        mean = per_seed.mean(axis=0)
        a = masked_choice(mean, mask)
        votes = Counter(masked_choice(p, mask) for p in per_seed)
        valid = np.where(mask, mean, 0.0)
        conf = float(valid[a] / valid.sum()) if valid.sum() > 0 else 0.0
        return a, {"action": Action(a).name, "confidence": round(conf, 4),
                   "probabilities": {Action(i).name: round(float(v), 4) for i, v in enumerate(mean)},
                   "seed_votes": {Action(k).name: v for k, v in votes.items()}}

    def act(self, raw: npt.NDArray[np.float64], mask: BoolArray) -> tuple[Action, dict[str, Any]]:
        """Action for one RAW observation (``build_observation`` order)."""
        x = self.normalizer.transform(np.asarray(raw, dtype=np.float64))
        a, info = self.act_normalized(x, mask)
        return Action(a), info

    def policy(self) -> Policy:
        return lambda obs, mask: self.act_normalized(obs, mask)[0]

    # Persistence ---------------------------------------------------------
    def save(self, directory: Path) -> dict[str, Any]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for s, p in zip(self.seeds, self.policies, strict=True):
            path = directory / f"{MODEL_KIND}_seed{s}.pt"
            torch.save(p.state_dict(), path)
            files[path.name] = sha256_file(path)
        npath = directory / "observation_normalizer.json"
        npath.write_text(json.dumps(self.normalizer.to_dict(), indent=1), encoding="utf-8")
        files[npath.name] = sha256_file(npath)
        return {"files": files, "rl": {"seeds": list(self.seeds), "ppo": asdict(self.ppo),
                                       "normalizer_file": npath.name, "obs_schema_hash": obs_schema()["hash"]}}

    @classmethod
    def load(cls, directory: Path, manifest: dict[str, Any]) -> PPOTradeManager:
        directory = Path(directory)
        meta, hashes = manifest["rl"], manifest["files"]
        if meta["obs_schema_hash"] != obs_schema()["hash"]:
            raise RLTrainingError("model observation schema does not match this code")

        def verified(name: str) -> Path:
            path = directory / name
            if hashes.get(name) is None or not path.is_file() or sha256_file(path) != hashes[name]:
                raise RLTrainingError(f"artifact {name} is missing from the manifest or was modified")
            return path

        normalizer = ObservationNormalizer.from_dict(json.loads(verified(meta["normalizer_file"]).read_text("utf-8")))
        ppo = PPOParams(**{**meta["ppo"], "net_arch": tuple(meta["ppo"]["net_arch"])})
        obs_space, act_space = _spaces(normalizer.clip)
        policies = []
        for s in meta["seeds"]:
            pol = ActorCriticPolicy(obs_space, act_space, lambda _: 0.0, **ppo.policy_kwargs())
            state = torch.load(verified(f"{MODEL_KIND}_seed{s}.pt"), map_location="cpu", weights_only=True)
            pol.load_state_dict(state)
            policies.append(pol)
        return cls(policies, meta["seeds"], normalizer, ppo)


# --------------------------------------------------------------------------- #
# Policies, rollouts and metrics
# --------------------------------------------------------------------------- #


def static_policy(obs: Float32Array, mask: BoolArray) -> int:
    """Baseline: take every valid setup, keep the strategy's initial stop and
    target (ENTER, then HOLD)."""
    return Action.ENTER.value if mask[Action.ENTER.value] else Action.HOLD.value


def skip_policy(obs: Float32Array, mask: BoolArray) -> int:
    return Action.SKIP_SETUP.value if mask[Action.SKIP_SETUP.value] else Action.HOLD.value


def random_valid_policy(seed: int) -> Policy:
    rng = np.random.default_rng(seed)
    return lambda obs, mask: int(rng.choice(np.flatnonzero(mask)))


@dataclass
class Rollout:
    trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: int = 0
    invalid: int = 0
    actions: Counter[str] = field(default_factory=Counter)
    reward: dict[str, float] = field(default_factory=dict)
    days: list[str] = field(default_factory=list)
    raw: list[npt.NDArray[np.float64]] = field(default_factory=list)


def rollout(replay: MarketReplay, days: Sequence[date], policy: Policy, *, normalizer: ObservationNormalizer | None,
            rewards: RewardWeights, keep_raw: bool = False) -> Rollout:
    """Run ``policy`` once on each day, in order (deterministic)."""
    out = Rollout()
    env = TradeManagementEnv(replay, days, rewards=rewards, normalizer=normalizer, sequential=True)
    for d in env.days:
        obs, info = env.reset(options={"day": d})
        out.days.append(d.isoformat())
        done = False
        while not done:
            if keep_raw:
                out.raw.append(observation_vector(env.raw_observation()))
            a = int(policy(obs, info["action_mask"]))
            out.actions[Action(a).name] += 1
            obs, _, done, _, info = env.step(a)
            out.decisions += 1
            out.invalid += int(info["violations"] > 0 and not any(
                t["exit_reason"] == "DAILY_LOSS_LIMIT" for t in info["closed"]))
            for k, v in info["reward_components"].items():
                out.reward[k] = out.reward.get(k, 0.0) + v
        out.trades.extend(env.trades)
    return out


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), n)


def trading_summary(r: Rollout, envelope: RiskEnvelope) -> dict[str, Any]:
    """Realised equity, trade quality, drawdown and risk compliance."""
    trades = sorted(r.trades, key=lambda t: t["exit_time"])
    net = np.array([t["net_pnl"] for t in trades], dtype=np.float64)
    wins, losses = net[net > 0], net[net < 0]
    eq = np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:] if net.size else np.zeros(0)
    max_dd = float(np.max(peak - eq)) if net.size else 0.0
    daily: dict[str, float] = {d: 0.0 for d in r.days}
    for t in trades:
        daily[t["day"]] = daily.get(t["day"], 0.0) + t["net_pnl"]
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    hold = [t["holding_minutes"] for t in trades]
    return {
        "days": len(r.days), "decisions": r.decisions, "trades": int(net.size), "wins": int(wins.size),
        "win_rate_pct": _r(wins.size / net.size * 100, 2) if net.size else None,
        "profit_factor": _r(float(wins.sum()) / -float(losses.sum()), 3) if losses.size else None,
        "expectancy": _r(float(net.mean()), 2) if net.size else None,
        "net_pnl": round(float(net.sum()), 2),
        "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2),
        "slippage": round(sum(t["slippage"] for t in trades), 2),
        "max_drawdown": round(max_dd, 2),
        "worst_day": round(min(daily.values()), 2) if daily else 0.0,
        "avg_r_multiple": _r(float(np.mean(rs)), 4) if rs else None,
        "avg_win": _r(float(wins.mean()), 2) if wins.size else None,
        "avg_loss": _r(float(losses.mean()), 2) if losses.size else None,
        "avg_holding_minutes": _r(float(np.mean(hold)), 2) if hold else None,
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "partial_exits": int(sum(t["partials"] for t in trades)),
        "actions": dict(r.actions), "invalid_actions": r.invalid,
        "invalid_action_rate": _r(r.invalid / r.decisions, 4) if r.decisions else 0.0,
        "reward": {k: round(v, 4) for k, v in r.reward.items()},
        "daily_pnl": {k: round(v, 2) for k, v in sorted(daily.items())},
        "compliance": compliance(trades, envelope),
    }


def compliance(trades: Sequence[dict[str, Any]], e: RiskEnvelope) -> dict[str, str]:
    """Independent re-check of the trade log against the risk limits
    (OK / WARN / BREACH). Any BREACH fails the model."""
    def ok(cond: bool, level: str = "BREACH") -> str:
        return "OK" if cond else level

    by_day: dict[str, list[dict[str, Any]]] = {}
    for t in sorted(trades, key=lambda t: t["entry_time"]):
        by_day.setdefault(t["day"], []).append(t)
    entry_after_block = False
    for ts in by_day.values():
        net, consec, last_loss = 0.0, 0, None
        for t in ts:
            sig = datetime.fromisoformat(t["signal_time"])
            if net <= -e.max_daily_loss or consec >= e.max_consecutive_losses or \
                    (last_loss is not None and sig < last_loss + timedelta(minutes=e.loss_cooldown_minutes)):
                entry_after_block = True
            net += t["net_pnl"]
            if t["net_pnl"] < 0:
                consec, last_loss = consec + 1, datetime.fromisoformat(t["exit_time"])
            else:
                consec = 0

    def tm(s: str) -> time:
        return datetime.fromisoformat(s).astimezone(IST).time()

    return {
        "every trade has a stop below entry": ok(all(0 < t["initial_stop"] < t["entry"] for t in trades)),
        "stops never widened": ok(all([s for _, s in t["stop_history"]] == sorted(s for _, s in t["stop_history"])
                                      and t["final_stop"] >= t["initial_stop"] for t in trades)),
        f"entries inside the window and before {e.no_new_entries_after:%H:%M}":
            ok(all(e.entry_start <= tm(t["entry_time"]) < e.no_new_entries_after for t in trades)),
        f"flat by the {e.square_off_start:%H:%M} square-off start":
            ok(all(tm(t["exit_time"]) <= e.square_off_start for t in trades)),
        f"at most {e.max_trades_per_day} trades per day": ok(all(len(v) <= e.max_trades_per_day
                                                                 for v in by_day.values())),
        "no entry after a daily-loss halt, in a cooldown or after the consecutive-loss limit":
            ok(not entry_after_block),
        "size within the per-trade risk budget": ok(all((t["entry"] - t["initial_stop"]) * t["quantity"]
                                                        <= e.risk_per_trade + 1e-6 for t in trades)),
        "day loss within the daily loss limit (gaps can overshoot)":
            ok(all(sum(t["net_pnl"] for t in v) >= -e.max_daily_loss for v in by_day.values()), "WARN"),
        "loss per trade within the risk budget (gaps can overshoot)":
            ok(all(t["net_pnl"] >= -e.risk_per_trade * 1.1 for t in trades), "WARN"),
    }


def compliance_status(c: dict[str, str]) -> str:
    vals = set(c.values())
    return "FAIL" if "BREACH" in vals else ("PASS WITH WARNINGS" if "WARN" in vals else "PASS")


def seed_spread(values: Sequence[float | None]) -> dict[str, Any]:
    v = [x for x in values if x is not None and math.isfinite(x)]
    if not v:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    return {"mean": _r(float(np.mean(v)), 2), "std": _r(float(np.std(v, ddof=1)) if len(v) > 1 else 0.0, 2),
            "min": _r(min(v), 2), "max": _r(max(v), 2), "n": len(v)}


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #


def fit_normalizer(replay: MarketReplay, days: Sequence[date], tcfg: RLTrainingConfig, seed: int
                   ) -> ObservationNormalizer:
    """Observation statistics from TRAINING days only (static policy plus
    random valid actions, so position states are covered)."""
    rows: list[npt.NDArray[np.float64]] = []
    rows += rollout(replay, days, static_policy, normalizer=None, rewards=tcfg.rewards, keep_raw=True).raw
    for k in range(tcfg.normalizer_random_episodes):
        rows += rollout(replay, days, random_valid_policy(seed * 1000 + k), normalizer=None, rewards=tcfg.rewards,
                        keep_raw=True).raw
    return ObservationNormalizer.fit(np.array(rows))


def train_policy(replay: MarketReplay, days: Sequence[date], normalizer: ObservationNormalizer, seed: int,
                 tcfg: RLTrainingConfig) -> ActorCriticPolicy:
    torch.set_num_threads(1)  # deterministic and fast for small networks
    env = TradeManagementEnv(replay, days, rewards=tcfg.rewards, normalizer=normalizer)
    model = PPO("MlpPolicy", env, seed=seed, device="cpu", verbose=0, policy_kwargs=tcfg.ppo.policy_kwargs(),
                **tcfg.ppo.sb3_kwargs())
    model.learn(total_timesteps=tcfg.total_timesteps)
    policy = model.policy
    policy.set_training_mode(False)
    return policy


def single_policy(policy: ActorCriticPolicy) -> Policy:
    def act(obs: Float32Array, mask: BoolArray) -> int:
        xt = torch.as_tensor(np.atleast_2d(obs), dtype=torch.float32)
        with torch.no_grad():
            probs = _action_probs(policy, xt)[0]
        return masked_choice(probs, mask)
    return act


Folds = list[tuple[list[date], list[date]]]


def walk_forward_splits(days: Sequence[date], n_folds: int, embargo_days: int, holdout_fraction: float,
                        min_holdout_days: int = 1) -> tuple[Folds, list[date], list[date]]:
    """(folds [(train, test)], development days, holdout days), chronological;
    every train day is before every test day, ``embargo_days`` apart."""
    days = sorted(days)
    n_hold = max(min_holdout_days, int(round(len(days) * holdout_fraction)))
    hold = days[len(days) - n_hold:]
    dev = days[:max(0, len(days) - n_hold - embargo_days)]
    if len(dev) < n_folds + 1 + n_folds * embargo_days or not hold:
        raise RLTrainingError(f"not enough days ({len(days)}) for {n_folds} walk-forward folds, an embargo of "
                              f"{embargo_days} day(s) and a holdout")
    blocks = [list(b) for b in np.array_split(np.array(dev, dtype=object), n_folds + 1)]
    folds = []
    for k in range(1, n_folds + 1):
        train = [d for b in blocks[:k] for d in b]
        test = blocks[k][embargo_days:]
        if not train or not test:
            raise RLTrainingError("a walk-forward fold is empty; add days or reduce folds / embargo")
        assert max(train) < min(test)
        folds.append((train, test))
    assert max(dev) < min(hold)
    return folds, dev, hold


@dataclass
class RLTrainingResult:
    model: PPOTradeManager
    evaluation: dict[str, Any]
    report_md: str
    status: ModelRunStatus
    registered: LoadedModel | None = None

    @property
    def gates_passed(self) -> bool:
        return bool(self.evaluation["gates_passed"])


def environment_info() -> dict[str, str]:
    return {"kind": "rl", "python": platform.python_version(), "torch": torch.__version__,
            "stable_baselines3": stable_baselines3.__version__, "gymnasium": gymnasium.__version__,
            "numpy": np.__version__, "rl_env": RL_ENV_VERSION, "obs_schema": OBS_SCHEMA_VERSION,
            "reward": REWARD_VERSION, "pipeline": PIPELINE_VERSION, "platform": platform.platform(terse=True)}


class RLTrainingPipeline:
    def __init__(self, cfg: BotConfig, registry: ModelRegistry | None = None) -> None:
        self.cfg = cfg
        self.registry = registry

    def run(self, dataset: HistoricalDataset, strategy_id: str, tcfg: RLTrainingConfig, *, register: bool = True,
            ml_score: Callable[[npt.NDArray[np.float64]], float] | None = None) -> RLTrainingResult:
        cfg = self.cfg
        replay = MarketReplay(cfg, dataset, strategy_id, setups=tcfg.setups, grid_every_minutes=tcfg.grid_every_minutes,
                              ml_score=ml_score)
        envelope = RiskEnvelope.from_config(cfg, strategy_id)
        probe = TradeManagementEnv(replay, dataset.days(), rewards=tcfg.rewards)
        folds, dev, hold = walk_forward_splits(probe.days, tcfg.n_folds, tcfg.embargo_days, tcfg.holdout_fraction,
                                               tcfg.min_holdout_days)

        def evaluate(days: Sequence[date], policy: Policy, norm: ObservationNormalizer | None) -> dict[str, Any]:
            return trading_summary(rollout(replay, days, policy, normalizer=norm, rewards=tcfg.rewards), envelope)

        # Walk-forward (out of sample) ----------------------------------------
        wf: list[dict[str, Any]] = []
        seed_oos_net: dict[int, float] = dict.fromkeys(tcfg.seeds, 0.0)
        for k, (train, test) in enumerate(folds):
            fold: dict[str, Any] = {"fold": k, "train": [train[0].isoformat(), train[-1].isoformat(), len(train)],
                                    "test": [test[0].isoformat(), test[-1].isoformat(), len(test)], "seeds": {}}
            for s in tcfg.seeds:
                norm = fit_normalizer(replay, train, tcfg, s)
                pol = train_policy(replay, train, norm, s, tcfg)
                m = evaluate(test, single_policy(pol), norm)
                fold["seeds"][str(s)] = m
                seed_oos_net[s] += m["net_pnl"]
            fold["baseline_static"] = evaluate(test, static_policy, None)
            fold["seed_spread_net_pnl"] = seed_spread([fold["seeds"][str(s)]["net_pnl"] for s in tcfg.seeds])
            wf.append(fold)

        # Final fit on the development period, holdout once -------------------
        # one frozen normaliser for the deployed ensemble, fitted on development days only
        norm = fit_normalizer(replay, dev, tcfg, tcfg.seeds[0])
        policies = [train_policy(replay, dev, norm, s, tcfg) for s in tcfg.seeds]
        model = PPOTradeManager(policies, tcfg.seeds, norm, tcfg.ppo)
        holdout = {
            "ensemble": evaluate(hold, model.policy(), norm),
            "seeds": {str(s): evaluate(hold, single_policy(p), norm) for s, p in zip(tcfg.seeds, policies,
                                                                                       strict=True)},
            "baseline_static": evaluate(hold, static_policy, None),
            "baseline_skip_all": evaluate(hold, skip_policy, None),
        }
        holdout["seed_spread_net_pnl"] = seed_spread([holdout["seeds"][str(s)]["net_pnl"] for s in tcfg.seeds])
        comparison = self._compare(strategy_id, hold, replay, envelope, tcfg, holdout["ensemble"])

        g = tcfg.gates
        ens = holdout["ensemble"]
        statuses = [compliance_status(f["seeds"][str(s)]["compliance"]) for f in wf for s in tcfg.seeds]
        statuses += [compliance_status(ens["compliance"])]
        gates = {
            "holdout trades": {"value": ens["trades"], "min": g.min_holdout_trades,
                               "passed": ens["trades"] >= g.min_holdout_trades},
            "holdout expectancy > 0 (net of costs)": {
                "value": ens["expectancy"],
                "passed": (not g.require_positive_expectancy) or (ens["expectancy"] or 0) > 0},
            "holdout net P&L beats the static-stop baseline": {
                "value": [ens["net_pnl"], holdout["baseline_static"]["net_pnl"]],
                "passed": (not g.require_beats_baseline) or ens["net_pnl"] > holdout["baseline_static"]["net_pnl"]},
            "seeds with positive walk-forward OOS net P&L": {
                "value": round(sum(v > 0 for v in seed_oos_net.values()) / len(seed_oos_net), 3),
                "min": g.min_positive_seed_fraction,
                "passed": sum(v > 0 for v in seed_oos_net.values()) / len(seed_oos_net)
                >= g.min_positive_seed_fraction},
            "holdout max drawdown": {"value": ens["max_drawdown"],
                                     "max": g.max_drawdown_daily_loss_multiple * envelope.max_daily_loss,
                                     "passed": ens["max_drawdown"]
                                     <= g.max_drawdown_daily_loss_multiple * envelope.max_daily_loss},
            "invalid action rate": {"value": ens["invalid_action_rate"], "max": g.max_invalid_action_rate,
                                    "passed": (ens["invalid_action_rate"] or 0) <= g.max_invalid_action_rate},
            "risk compliance (every OOS run and the holdout)": {"value": sorted(set(statuses)),
                                                                "passed": "FAIL" not in statuses},
        }
        evaluation: dict[str, Any] = {
            "model_kind": MODEL_KIND, "strategy_id": strategy_id, "data_source": dataset.source,
            "data_description": dataset.description, "synthetic": dataset.is_synthetic,
            "windows": {"development": [dev[0].isoformat(), dev[-1].isoformat(), len(dev)],
                        "holdout": [hold[0].isoformat(), hold[-1].isoformat(), len(hold)],
                        "embargo_days": tcfg.embargo_days},
            "setups": {"source": tcfg.setups, "dropped": replay.dropped},
            "walk_forward": wf, "walk_forward_seed_net_pnl": {str(k): round(v, 2) for k, v in seed_oos_net.items()},
            "holdout": holdout, "comparison": comparison, "gates": gates,
            "gates_passed": all(v["passed"] for v in gates.values()),
            "judged_on": "realised equity, trade quality, drawdown and risk compliance (reward is reported only)",
            "oos_events": sum(f["seeds"][str(s)]["decisions"] for f in wf for s in tcfg.seeds),
            "holdout_events": ens["decisions"],
        }
        evaluation["headline"] = {
            "holdout_net_pnl": ens["net_pnl"], "holdout_trades": ens["trades"], "holdout_expectancy": ens["expectancy"],
            "holdout_max_drawdown": ens["max_drawdown"], "baseline_static_net_pnl":
                holdout["baseline_static"]["net_pnl"], "compliance": compliance_status(ens["compliance"]),
            "seed_spread_net_pnl": holdout["seed_spread_net_pnl"]}
        status = ModelRunStatus.TRAINED if evaluation["gates_passed"] else ModelRunStatus.REJECTED
        report = render_report(evaluation)
        result = RLTrainingResult(model, evaluation, report, status)
        if register and self.registry is not None:
            start = datetime.combine(dev[0], cfg.settings.market_session.market_open, tzinfo=IST)
            end = datetime.combine(dev[-1], cfg.risk.time_rules.square_off_start, tzinfo=IST)
            manifest = {
                "feature_schema": obs_schema(), "training_window": {"start": start.isoformat(),
                                                                    "end": end.isoformat()},
                "holdout_window": {"start": hold[0].isoformat(), "end": hold[-1].isoformat()},
                "hyperparameters": tcfg.hyperparameters(), "environment": environment_info(),
                "environment_spec": environment_spec(cfg, strategy_id, tcfg.rewards, tcfg.setups),
                "evaluation": evaluation, "data": {"source": dataset.source, "description": dataset.description},
                "pipeline_version": PIPELINE_VERSION,
            }
            reg = self.registry.register(model, kind=MODEL_KIND, strategy_id=strategy_id, data_source=dataset.source,
                                         manifest=manifest, report_md=report, status=status)
            result.registered = reg
            if evaluation["gates_passed"] and not dataset.is_synthetic:
                self.registry.mark_validated(reg.record.run_id)
        return result

    def _compare(self, strategy_id: str, hold: Sequence[date], replay: MarketReplay, envelope: RiskEnvelope,
                 tcfg: RLTrainingConfig, new: dict[str, Any]) -> dict[str, Any]:
        """Holdout comparison with the current production RL model (HISTORICAL, deployable)."""
        if self.registry is None:
            return {"production": None, "note": "no registry"}
        try:
            prod = self.registry.load_production(ModelType.RL_PPO, strategy_id)
        except Exception as exc:
            return {"production": None, "note": f"production model could not be loaded: {exc}"}
        if prod is None:
            return {"production": None, "note": "no production RL model for this strategy"}
        m: PPOTradeManager = prod.model
        old = trading_summary(rollout(replay, hold, m.policy(), normalizer=m.normalizer, rewards=tcfg.rewards),
                              envelope)
        return {"production": prod.record.model_version, "production_holdout": old,
                "new_beats_production": new["net_pnl"] > old["net_pnl"]
                and compliance_status(new["compliance"]) != "FAIL"}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _row(name: str, m: dict[str, Any]) -> str:
    def v(k: str) -> Any:
        return "-" if m[k] is None else m[k]
    return (f"| {name} | {m['trades']} | {m['net_pnl']:,.0f} | {v('expectancy')} | {v('win_rate_pct')} | "
            f"{v('profit_factor')} | {m['max_drawdown']:,.0f} | {v('avg_holding_minutes')} | "
            f"{compliance_status(m['compliance'])} |")


def render_report(ev: dict[str, Any]) -> str:
    lines = []
    if ev["synthetic"]:
        lines += [f"> **{SYNTHETIC_BANNER}**", ""]
    lines += [f"# RL trade manager evaluation - {ev['strategy_id']}", "",
              f"Data: **{ev['data_source']}** ({ev['data_description']})  ",
              f"Development: {ev['windows']['development'][0]} .. {ev['windows']['development'][1]} "
              f"({ev['windows']['development'][2]} days); holdout: {ev['windows']['holdout'][0]} .. "
              f"{ev['windows']['holdout'][1]} ({ev['windows']['holdout'][2]} days); embargo "
              f"{ev['windows']['embargo_days']} day(s)  ",
              f"Judged on: {ev['judged_on']}", "",
              "## Holdout", "",
              "| policy | trades | net P&L | expectancy | win % | profit factor | max DD | avg hold (min) | "
              "compliance |", "|---|---|---|---|---|---|---|---|---|"]
    h = ev["holdout"]
    lines.append(_row("PPO seed ensemble", h["ensemble"]))
    for s, m in h["seeds"].items():
        lines.append(_row(f"seed {s}", m))
    lines.append(_row("baseline: static stop/target", h["baseline_static"]))
    lines.append(_row("baseline: skip every setup", h["baseline_skip_all"]))
    lines += ["", f"Seed spread of holdout net P&L: {h['seed_spread_net_pnl']}", "",
              f"Ensemble actions: {h['ensemble']['actions']}  ", f"Exit reasons: {h['ensemble']['exit_reasons']}  ",
              f"Reward (reported, not judged): {h['ensemble']['reward']}", "", "## Walk-forward (out of sample)", ""]
    for f in ev["walk_forward"]:
        lines += [f"Fold {f['fold']}: train {f['train'][0]}..{f['train'][1]} ({f['train'][2]} d), test "
                  f"{f['test'][0]}..{f['test'][1]} ({f['test'][2]} d); seed net P&L {f['seed_spread_net_pnl']}; "
                  f"static baseline {f['baseline_static']['net_pnl']:,.0f}", ""]
    lines += ["## Risk compliance (holdout ensemble)", ""]
    lines += [f"- {k}: **{v}**" for k, v in h["ensemble"]["compliance"].items()]
    lines += ["", "## Gates", ""]
    lines += [f"- {k}: {'PASS' if v['passed'] else 'FAIL'} (value {v['value']})" for k, v in ev["gates"].items()]
    lines += ["", f"All gates passed: **{ev['gates_passed']}**", "",
              f"Comparison with production: {ev['comparison'].get('note') or ev['comparison'].get('production')}"]
    if ev["synthetic"]:
        lines += ["", f"> {SYNTHETIC_BANNER}"]
    return "\n".join(lines) + "\n"


__all__ = ["MODEL_KIND", "PPOParams", "PPOTradeManager", "RLGates", "RLTrainingConfig", "RLTrainingError",
           "RLTrainingPipeline", "RLTrainingResult", "Rollout", "compliance", "compliance_status", "environment_info",
           "fit_normalizer", "masked_choice", "render_report", "rollout", "seed_spread", "single_policy",
           "skip_policy", "static_policy", "train_policy", "trading_summary", "walk_forward_splits"]
