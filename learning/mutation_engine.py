"""MUTATE stage of the Reflective Loop: whitelisted parameter changes only.

A candidate is the strategy's current config with a few WHITELISTED leaves
changed and a new patch version. Nothing else can change:

* ``WHITELIST`` is the complete list of mutable parameters, each with its
  build-plan category, bounds (per mode where the unit depends on a mode),
  granularity, and the largest change one mutation may make. Categories:
  entry thresholds, confirmation conditions, stop-loss %, target R-multiple,
  trailing activation, trailing step, trade-size multiplier (reduce-only),
  time windows, strike-selection rules, ML confidence threshold.
* ``NEVER_MUTABLE`` names what is refused outright: capital allocation,
  every risk limit (per-strategy and global daily loss, exposure / open
  trades, trades per day, cooldown, consecutive losses), the risk budget per
  trade, the kill switch, square-off and entry cut-off times, credentials,
  enable flags, the RL / ML switches and the signal instrument. Global
  settings (risk_limits.yaml, settings.yaml, .env) are outside a strategy
  config, so a mutation cannot even address them.
* After applying the change, the WHOLE configuration is re-validated
  (``BotConfig``: hard limits, per-strategy limits within global limits,
  entry window inside the session and before the entry cut-off), the
  strategy class is constructed with it (its typed params), and the full
  before / after configs are diffed leaf by leaf: any changed leaf outside the
  requested whitelisted paths (plus ``version``) refuses the candidate.

``apply_overlay`` uses the same diff to put a stored version back on top of
the YAML config (PAPER deployments, verification runs).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Literal

from config.settings import BotConfig, StrategyConfig, StrategyDefaults
from core.clock import now_ist
from strategies.registry import STRATEGY_CLASSES

Kind = Literal["float", "int", "time", "choice"]


class MutationRefused(ValueError):
    """The requested change is not allowed (not whitelisted, out of bounds,
    or it breaks a hard limit)."""


# --------------------------------------------------------------------------- #
# The whitelist
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParamSpec:
    path: str  # dotted path inside one StrategyConfig
    category: str  # build-plan whitelist category
    kind: Kind
    #: bounds and step: {mode value or "*": (lo, hi, step)}; for "time": minutes since midnight
    bounds: Mapping[str, tuple[float, float, float]] = field(default_factory=dict)
    mode_path: str | None = None  # the leaf whose value selects the bounds (e.g. "stop_loss.mode")
    choices: tuple[str, ...] = ()
    strategies: frozenset[str] | None = None  # None = every strategy
    #: largest change one mutation may make: relative (0.5 = +/-50%) for numbers,
    #: absolute minutes for times; None = any value within bounds
    max_change: float | None = None
    only_when: Callable[[Mapping[str, Any]], bool] | None = None  # extra applicability rule
    note: str = ""

    def applies_to(self, strategy_id: str, data: Mapping[str, Any]) -> bool:
        if self.strategies is not None and strategy_id not in self.strategies:
            return False
        return self.only_when is None or self.only_when(data)

    def limits(self, data: Mapping[str, Any]) -> tuple[float, float, float] | None:
        if self.kind == "choice":
            return None
        key = str(get_path(data, self.mode_path)) if self.mode_path else "*"
        return self.bounds.get(key) or self.bounds.get("*")


def _r_target(data: Mapping[str, Any]) -> bool:
    return bool(get_path(data, "take_profit.mode") == "R_MULTIPLE")


def _trailing_on(data: Mapping[str, Any]) -> bool:
    return bool(get_path(data, "trailing.enabled"))


def _ml_on(data: Mapping[str, Any]) -> bool:
    return bool(get_path(data, "ml.enabled"))


ORB = frozenset({"orb_options"})
VWAP = frozenset({"vwap_ema"})

WHITELIST: dict[str, ParamSpec] = {s.path: s for s in (
    # Entry thresholds
    ParamSpec("params.breakout_buffer_points", "entry_threshold", "float", {"*": (0.0, 30.0, 0.5)},
              strategies=ORB, max_change=1.0, note="points beyond the opening range a close must reach"),
    ParamSpec("params.min_range_points", "entry_threshold", "float", {"*": (5.0, 60.0, 1.0)}, strategies=ORB,
              max_change=0.5),
    ParamSpec("params.max_range_pct", "entry_threshold", "float", {"*": (0.5, 3.0, 0.1)}, strategies=ORB,
              max_change=0.5),
    ParamSpec("params.min_volume_ratio", "entry_threshold", "float", {"*": (0.8, 3.0, 0.05)}, strategies=VWAP,
              max_change=0.5),
    ParamSpec("params.max_vwap_crosses", "entry_threshold", "int", {"*": (1, 10, 1)}, strategies=VWAP,
              max_change=0.5),
    # Confirmation conditions
    ParamSpec("params.momentum_bars", "confirmation", "int", {"*": (1, 5, 1)}, strategies=VWAP, max_change=1.0),
    ParamSpec("params.chop_lookback_bars", "confirmation", "int", {"*": (6, 30, 1)}, strategies=VWAP,
              max_change=0.5),
    # Stop-loss percentage / points
    ParamSpec("stop_loss.value", "stop_loss", "float",
              {"PREMIUM_PERCENT": (10.0, 50.0, 1.0), "POINTS": (2.0, 100.0, 0.5)}, mode_path="stop_loss.mode",
              max_change=0.4),
    # Target R-multiple
    ParamSpec("take_profit.value", "target_r_multiple", "float", {"R_MULTIPLE": (1.0, 5.0, 0.25)},
              mode_path="take_profit.mode", only_when=_r_target, max_change=0.5),
    # Trailing activation threshold / step
    ParamSpec("trailing.activation", "trailing_activation", "float",
              {"R_MULTIPLE": (0.25, 3.0, 0.25), "PREMIUM_PERCENT": (5.0, 60.0, 1.0), "POINTS": (2.0, 100.0, 0.5),
               "STRUCTURAL": (0.0, 3.0, 0.25)}, mode_path="trailing.mode", only_when=_trailing_on, max_change=0.5),
    ParamSpec("trailing.step", "trailing_step", "float",
              {"R_MULTIPLE": (0.1, 2.0, 0.05), "PREMIUM_PERCENT": (1.0, 25.0, 0.5), "POINTS": (1.0, 50.0, 0.5),
               "STRUCTURAL": (0.1, 2.0, 0.05)}, mode_path="trailing.mode", only_when=_trailing_on, max_change=0.5),
    # Trade-size multiplier within limits (reduce-only: the schema caps it at 1.0)
    ParamSpec("sizing.size_multiplier", "size_multiplier", "float", {"*": (0.25, 1.0, 0.05)}, max_change=0.5,
              note="can only shrink positions relative to the fixed risk budget"),
    # Time windows (inside the session and before the entry cut-off: re-checked by BotConfig)
    ParamSpec("entry_window.start", "time_window", "time", {"*": (9 * 60 + 15, 14 * 60 + 30, 5)}, max_change=60),
    ParamSpec("entry_window.end", "time_window", "time", {"*": (9 * 60 + 45, 14 * 60 + 45, 5)}, max_change=60),
    # Strike-selection rules
    ParamSpec("strike_selection.mode", "strike_selection", "choice", choices=("ATM", "ITM", "OTM")),
    ParamSpec("strike_selection.offset_steps", "strike_selection", "int", {"*": (0, 2, 1)}),
    ParamSpec("strike_selection.min_premium", "strike_selection", "float", {"*": (10.0, 200.0, 5.0)},
              max_change=0.5),
    # ML confidence threshold (only meaningful with the ML filter on)
    ParamSpec("ml.confidence_threshold", "ml_confidence_threshold", "float", {"*": (0.5, 0.8, 0.01)},
              only_when=_ml_on, max_change=0.15),
)}

#: Refused outright, whatever the request (prefix match on the dotted path).
NEVER_MUTABLE: tuple[str, ...] = (
    "allocated_capital",          # capital allocation
    "risk",                       # per-strategy daily loss, open trades, trades/day, cooldown, consecutive losses
    "sizing.risk_per_trade",      # risk budget per trade
    "enabled", "name", "version", "timeframe_minutes",
    "rl",                         # the RL switch and its action bounds
    "ml.enabled", "ml.reduce_below", "ml.min_size_multiplier", "ml.delay_band",
    "params.signal_instrument", "params.max_entries_per_direction_per_day",
    "params.opening_range_start", "params.opening_range_end",
    "strike_selection.liquidity", "strike_selection.strike_step", "strike_selection.max_premium",
    "stop_loss.mode", "take_profit.mode", "trailing.mode", "trailing.enabled",
    # not part of a strategy config at all (named so that a request for them is refused loudly)
    "capital", "global", "per_strategy", "per_trade", "max_exposure", "kill_switch", "time_rules", "square_off",
    "no_new_entries_after", "credentials", "env", "upstox", "telegram", "rate_limits", "mode", "autonomy",
)

CATEGORIES = tuple(dict.fromkeys(s.category for s in WHITELIST.values()))


def never_mutable(path: str) -> bool:
    return any(path == p or path.startswith(p + ".") for p in NEVER_MUTABLE)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


_SECONDS_TIME = re.compile(r"^(\d{2}:\d{2}):00$")


def canonical(data: Any) -> Any:
    """JSON-safe config with times as 'HH:MM' (the form the YAML loader accepts)."""
    if isinstance(data, Mapping):
        return {str(k): canonical(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [canonical(v) for v in data]
    if isinstance(data, time):
        return data.strftime("%H:%M")
    if isinstance(data, str) and (m := _SECONDS_TIME.match(data)):
        return m.group(1)
    return data


def dump_strategy(scfg: StrategyConfig) -> dict[str, Any]:
    out: dict[str, Any] = canonical(scfg.model_dump(mode="json"))
    return out


def get_path(data: Mapping[str, Any], path: str | None) -> Any:
    if path is None:
        return None
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            raise MutationRefused(f"{path}: no such setting")
        cur = nxt
    if parts[-1] not in cur:
        raise MutationRefused(f"{path}: no such setting")
    cur[parts[-1]] = value


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(data, Mapping):
        out: dict[str, Any] = {}
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    return {prefix[:-1]: data}


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    a, b = flatten(before), flatten(after)
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def _hhmm(v: Any) -> int:
    if isinstance(v, time):
        return v.hour * 60 + v.minute
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", str(v).strip())
    if not m:
        raise MutationRefused(f"invalid time {v!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def bump_patch(version: str, taken: Iterable[str] = ()) -> str:
    used = set(taken)
    major, minor, patch = (int(x) for x in version.split("."))
    while True:
        patch += 1
        v = f"{major}.{minor}.{patch}"
        if v not in used:
            return v


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Mutation:
    path: str
    old: Any
    new: Any
    category: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "old": self.old, "new": self.new, "category": self.category}


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    base_version: str
    version: str
    mutations: tuple[Mutation, ...]
    config: StrategyConfig  # the candidate strategy config (fully validated)
    bot_config: BotConfig  # the whole bot config with the candidate in place

    @property
    def config_json(self) -> dict[str, Any]:
        return dump_strategy(self.config)

    def describe(self) -> str:
        return ", ".join(f"{m.path} {m.old} -> {m.new}" for m in self.mutations)


class MutationEngine:
    def __init__(self, cfg: BotConfig, *, now_fn: Callable[[], Any] = now_ist) -> None:
        self.cfg = cfg
        self._now = now_fn

    # Validation ------------------------------------------------------------
    def check_value(self, strategy_id: str, base: Mapping[str, Any], path: str, value: Any) -> Mutation:
        if never_mutable(path):
            raise MutationRefused(f"{path} is never mutable (capital, risk limits, kill switch, square-off, "
                                  "credentials and switches are outside the learning loop)")
        spec = WHITELIST.get(path)
        if spec is None:
            raise MutationRefused(f"{path} is not a whitelisted parameter")
        if not spec.applies_to(strategy_id, base):
            raise MutationRefused(f"{path} does not apply to {strategy_id} with its current settings")
        old = get_path(base, path)
        if old is None:
            raise MutationRefused(f"{path}: {strategy_id} has no such setting")
        new = self._coerce(spec, base, value)
        if (_hhmm(new) == _hhmm(old)) if spec.kind == "time" else (new == old):
            raise MutationRefused(f"{path}: {new} is the current value")
        if spec.max_change is not None:
            if spec.kind == "time":
                if abs(_hhmm(new) - _hhmm(old)) > spec.max_change:
                    raise MutationRefused(f"{path}: {old} -> {new} moves more than {spec.max_change:g} minutes")
            elif spec.kind in ("float", "int") and float(old) != 0:
                rel = abs(float(new) - float(old)) / abs(float(old))
                if rel > spec.max_change + 1e-9:
                    raise MutationRefused(f"{path}: {old} -> {new} changes it by {rel:.0%} "
                                          f"(at most {spec.max_change:.0%} per mutation)")
        return Mutation(path, old, new, spec.category)

    @staticmethod
    def _coerce(spec: ParamSpec, base: Mapping[str, Any], value: Any) -> Any:
        if spec.kind == "choice":
            if str(value) not in spec.choices:
                raise MutationRefused(f"{spec.path}: {value!r} is not one of {spec.choices}")
            return str(value)
        lim = spec.limits(base)
        if lim is None:
            raise MutationRefused(f"{spec.path}: no bounds for mode {get_path(base, spec.mode_path)!r}")
        lo, hi, step = lim
        if spec.kind == "time":
            m = _hhmm(value)
            if not lo <= m <= hi or m % int(step):
                raise MutationRefused(f"{spec.path}: {value} outside {_fmt_time(int(lo))}-{_fmt_time(int(hi))} "
                                      f"in {int(step)}-minute steps")
            return _fmt_time(m)
        try:
            x = float(value)
        except (TypeError, ValueError) as exc:
            raise MutationRefused(f"{spec.path}: {value!r} is not a number") from exc
        if isinstance(value, bool) or not math.isfinite(x):
            raise MutationRefused(f"{spec.path}: {value!r} is not a number")
        if not lo - 1e-9 <= x <= hi + 1e-9:
            raise MutationRefused(f"{spec.path}: {x:g} outside [{lo:g}, {hi:g}]")
        steps = round((x - lo) / step)
        if abs(lo + steps * step - x) > 1e-6:
            raise MutationRefused(f"{spec.path}: {x:g} is not on the {step:g} grid")
        if spec.kind == "int":
            if x != int(x):
                raise MutationRefused(f"{spec.path}: {x:g} must be a whole number")
            return int(x)
        return round(x, 6)

    def _validated(self, strategy_id: str, data: dict[str, Any]) -> tuple[StrategyConfig, BotConfig]:
        all_data = {sid: dump_strategy(s) for sid, s in self.cfg.strategies.strategies.items()}
        all_data[strategy_id] = canonical(data)
        try:
            strategies = StrategyDefaults.model_validate({"strategies": all_data})
            bot_cfg = BotConfig(project_root=self.cfg.project_root, settings=self.cfg.settings, risk=self.cfg.risk,
                                strategies=strategies, env=self.cfg.env)
        except ValueError as exc:
            raise MutationRefused(f"the candidate breaks a configuration rule or hard limit: {exc}") from exc
        scfg = bot_cfg.strategies.strategies[strategy_id]
        cls = STRATEGY_CLASSES.get(strategy_id)
        if cls is not None:
            try:  # the strategy's own typed params must accept it
                cls(strategy_id, scfg, signal_instrument_key="VALIDATION|KEY", now_fn=self._now)
            except Exception as exc:
                raise MutationRefused(f"{strategy_id} refuses the candidate params: {exc}") from exc
        return scfg, bot_cfg

    def _guard_diff(self, base: Mapping[str, Any], after: Mapping[str, Any], allowed: Iterable[str]) -> None:
        allow = set(allowed) | {"version"}
        for leaf in diff(base, after):
            if leaf not in allow or (leaf != "version" and (never_mutable(leaf) or leaf not in WHITELIST)):
                raise MutationRefused(f"{leaf} would change, but only {sorted(set(allowed))} may")

    # Public ------------------------------------------------------------------
    def propose(self, strategy_id: str, changes: Mapping[str, Any], *, taken_versions: Iterable[str] = (),
                version: str | None = None) -> Candidate:
        base_cfg = self.cfg.strategies.strategies.get(strategy_id)
        if base_cfg is None:
            raise MutationRefused(f"unknown strategy {strategy_id!r}")
        if not changes:
            raise MutationRefused("a candidate needs at least one change")
        base = dump_strategy(base_cfg)
        mutations = [self.check_value(strategy_id, base, path, value) for path, value in changes.items()]
        data = json.loads(json.dumps(base))
        for m in mutations:
            set_path(data, m.path, m.new)
        taken = {base_cfg.version, *taken_versions}
        new_version = version or bump_patch(base_cfg.version, taken)
        if new_version in taken:
            raise MutationRefused(f"version {new_version} already exists for {strategy_id}")
        data["version"] = new_version
        scfg, bot_cfg = self._validated(strategy_id, data)
        self._guard_diff(base, dump_strategy(scfg), [m.path for m in mutations])
        return Candidate(strategy_id, base_cfg.version, new_version, tuple(mutations), scfg, bot_cfg)

    def apply_overlay(self, strategy_id: str, config_json: Mapping[str, Any]) -> BotConfig:
        """The bot config with a stored strategy version in place of the YAML
        one. Refused unless it differs from the YAML config only in whitelisted
        leaves (and its version) and passes every validator."""
        base_cfg = self.cfg.strategies.strategies.get(strategy_id)
        if base_cfg is None:
            raise MutationRefused(f"unknown strategy {strategy_id!r}")
        base = dump_strategy(base_cfg)
        config_json = canonical(config_json)
        changed = [k for k in diff(base, config_json) if k != "version"]
        self._guard_diff(base, config_json, changed)
        for leaf in changed:
            self.check_bounds(strategy_id, base, leaf, get_path(config_json, leaf))
        _, bot_cfg = self._validated(strategy_id, json.loads(json.dumps(dict(config_json))))
        return bot_cfg

    def check_bounds(self, strategy_id: str, base: Mapping[str, Any], path: str, value: Any) -> None:
        """Whitelist and bounds only (an overlay may differ by several mutations)."""
        if never_mutable(path) or path not in WHITELIST:
            raise MutationRefused(f"{path} is not mutable")
        spec = WHITELIST[path]
        if not spec.applies_to(strategy_id, base):
            raise MutationRefused(f"{path} does not apply to {strategy_id}")
        self._coerce(spec, base, value)

    def mutable_parameters(self, strategy_id: str) -> list[ParamSpec]:
        scfg = self.cfg.strategies.strategies[strategy_id]
        data = dump_strategy(scfg)
        return [s for s in WHITELIST.values() if s.applies_to(strategy_id, data) and get_path(data, s.path) is not None]


def describe_whitelist() -> list[dict[str, Any]]:
    out = []
    for s in WHITELIST.values():
        bounds = {k: list(v) for k, v in s.bounds.items()}
        out.append({"path": s.path, "category": s.category, "kind": s.kind, "bounds": bounds,
                    "choices": list(s.choices), "strategies": sorted(s.strategies) if s.strategies else "all",
                    "max_change": s.max_change, "note": s.note})
    return out


__all__: Sequence[str] = ("CATEGORIES", "NEVER_MUTABLE", "WHITELIST", "Candidate", "Mutation", "MutationEngine",
                          "MutationRefused", "ParamSpec", "bump_patch", "canonical", "dump_strategy",
                          "describe_whitelist", "diff", "flatten",
                          "get_path", "never_mutable", "set_path")
