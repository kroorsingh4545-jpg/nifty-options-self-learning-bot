"""HYPOTHESIZE stage of the Reflective Loop.

Deterministic rules turn a strategy's reflection into concrete, testable
hypotheses. Every hypothesis states:

* the proposed change (whitelisted parameters only; each value is checked
  with ``MutationEngine.check_value`` before the hypothesis is emitted)
* the expected effect and the evidence it rests on
* the affected strategy and its base version
* the test metric (OOS cost-adjusted expectancy, or max drawdown for a
  risk-reducing change) and the minimum OOS sample size
* the validation plan (the VERIFY gates)
* the risk impact and the rollback plan

A rule that needs a non-whitelisted change (e.g. a regime filter, which is
strategy LOGIC) produces an observation, never a hypothesis. Hypotheses built
from SYNTHETIC / UNKNOWN evidence are flagged ``research_only``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from config.settings import BotConfig
from core.ids import new_uid
from learning.mutation_engine import WHITELIST, MutationEngine, MutationRefused, get_path
from learning.reflection_engine import StrategyReflection

#: The gates every candidate must pass (learning.validation_engine).
VALIDATION_GATES = ("MIN_SAMPLE", "RISK_COMPLIANCE", "BACKTEST", "WALK_FORWARD", "OUT_OF_SAMPLE", "STRESS",
                    "MULTI_SEED", "PRODUCTION_COMPARISON", "NO_TRADE_BASELINE")

METRIC_EXPECTANCY = "oos_expectancy_rs"
METRIC_DRAWDOWN = "oos_max_drawdown_rs"


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    rule: str
    strategy_id: str
    base_version: str
    title: str
    proposed_change: dict[str, Any]  # dotted path -> new value (whitelisted)
    current_values: dict[str, Any]
    expected_effect: str
    evidence: tuple[str, ...]
    test_metric: str
    min_sample_size: int
    validation_plan: dict[str, Any]
    risk_impact: str
    rollback_plan: str
    research_only: bool  # built from SYNTHETIC / UNKNOWN evidence
    strength: float  # evidence strength used for ranking (trades x rupees)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = list(self.evidence)
        return d


@dataclass
class GenerationResult:
    hypotheses: list[Hypothesis] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)  # findings that need a non-whitelisted change
    skipped: list[str] = field(default_factory=list)  # rule fired but its change was refused


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _grid(path: str, data: Mapping[str, Any], x: float) -> float | None:
    spec = WHITELIST[path]
    lim = spec.limits(data)
    if lim is None:
        return None
    lo, hi, step = lim
    x = min(max(x, lo), hi)
    snapped = lo + round((x - lo) / step) * step
    return round(min(max(snapped, lo), hi), 6)


def _hhmm(v: Any) -> int:
    h, m = str(v).split(":")[:2]
    return int(h) * 60 + int(m)


def _fmt(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _bucket(r: StrategyReflection, dim: str, key: str) -> dict[str, Any]:
    return r.breakdowns.get(dim, {}).get(key) or {"trades": 0, "expectancy": None, "avg_minutes": None}


def _share(r: StrategyReflection, dim: str, key: str) -> float:
    n = r.stats.trades
    return _bucket(r, dim, key)["trades"] / n if n else 0.0


RISK_NOTE = ("Rupee risk per trade is unchanged: position size still comes from the fixed per-trade risk budget, "
             "and every per-strategy / global loss limit, the kill switch and the square-off are untouched.")


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

Proposal = tuple[str, dict[str, Any], str, tuple[str, ...], str, str, float]
# (rule, change, title, evidence, expected effect, test metric, strength)
Rule = Callable[[StrategyReflection, Mapping[str, Any], int], Proposal | None]


def rule_stops_too_tight(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    stop = _bucket(r, "exit_reason", "STOP_LOSS")
    share = _share(r, "exit_reason", "STOP_LOSS")
    if stop["trades"] < min_n or share < 0.5 or (stop["avg_minutes"] or 1e9) > 20:
        return None
    new = _grid("stop_loss.value", data, float(get_path(data, "stop_loss.value")) * 1.2)
    ev = (f"{stop['trades']} of {r.stats.trades} trades ({share:.0%}) exited at the stop after "
          f"{stop['avg_minutes']} minutes on average",)
    return ("STOPS_TOO_TIGHT", {"stop_loss.value": new}, "Widen the stop: stops are hit quickly", ev,
            "Fewer stop-outs from noise; higher win rate; fewer lots per trade at the same rupee risk",
            METRIC_EXPECTANCY, stop["trades"] * abs(stop["expectancy"] or 0))


def rule_target_too_far(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    if get_path(data, "take_profit.mode") != "R_MULTIPLE" or r.stats.trades < 2 * min_n:
        return None
    hit = _share(r, "exit_reason", "TARGET")
    late = [_bucket(r, "exit_reason", k) for k in ("SQUARE_OFF", "TRAILING_STOP")]
    late_n = sum(b["trades"] for b in late)
    late_pos = sum(b["trades"] for b in late if (b["expectancy"] or 0) > 0)
    if hit >= 0.15 or late_n < min_n or late_pos == 0:
        return None
    cur = float(get_path(data, "take_profit.value"))
    new = _grid("take_profit.value", data, cur - 0.5)
    if new is None or new >= cur:
        return None
    ev = (f"only {hit:.0%} of trades reached the {cur:g}R target; {late_n} closed later at the trail or the "
          "square-off",)
    return ("TARGET_TOO_FAR", {"take_profit.value": new}, "Bring the target closer: it is rarely reached", ev,
            "More trades bank profit at the target instead of giving it back", METRIC_EXPECTANCY,
            late_n * abs(r.stats.expectancy or 0))


def rule_weak_time_window(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    rows = r.breakdowns.get("entry_time", {})
    keys = sorted(rows)
    if len(keys) < 2:
        return None
    first, last = rows[keys[0]], rows[keys[-1]]
    start, end = _hhmm(get_path(data, "entry_window.start")), _hhmm(get_path(data, "entry_window.end"))
    if last["trades"] >= min_n and (last["expectancy"] or 0) < 0 and (r.stats.expectancy or 0) > (last["expectancy"]
                                                                                                    or 0):
        new_end = max(start + 30, end - 30)
        if new_end < end:
            ev = (f"entries in {keys[-1]} lost {last['expectancy']} per trade over {last['trades']} trades",)
            return ("LATE_ENTRIES_LOSE", {"entry_window.end": _fmt(new_end)},
                    f"Stop entries earlier ({_fmt(end)} -> {_fmt(new_end)})", ev,
                    "Skip the weakest part of the entry window", METRIC_EXPECTANCY,
                    last["trades"] * abs(last["expectancy"] or 0))
    if first["trades"] >= min_n and (first["expectancy"] or 0) < 0 and (r.stats.expectancy or 0) > (
            first["expectancy"] or 0):
        new_start = min(end - 30, start + 15)
        if new_start > start:
            ev = (f"entries in {keys[0]} lost {first['expectancy']} per trade over {first['trades']} trades",)
            return ("EARLY_ENTRIES_LOSE", {"entry_window.start": _fmt(new_start)},
                    f"Start entries later ({_fmt(start)} -> {_fmt(new_start)})", ev,
                    "Skip the noisy first minutes of the window", METRIC_EXPECTANCY,
                    first["trades"] * abs(first["expectancy"] or 0))
    return None


def rule_false_breakouts(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    if r.stats.trades < 2 * min_n or (r.stats.win_rate_pct or 100) >= 40 or _share(r, "exit_reason",
                                                                                    "STOP_LOSS") < 0.5:
        return None
    ev = (f"win rate {r.stats.win_rate_pct}% with {_share(r, 'exit_reason', 'STOP_LOSS'):.0%} of exits at the stop",)
    if r.strategy_id == "orb_options":
        cur = float(get_path(data, "params.breakout_buffer_points"))
        new = _grid("params.breakout_buffer_points", data, cur * 1.5 if cur > 0 else 2.0)
        if new is None or new <= cur:
            return None
        return ("FALSE_BREAKOUTS", {"params.breakout_buffer_points": new},
                "Demand a stronger breakout (bigger buffer)", ev,
                "Fewer false breakouts, fewer but better entries", METRIC_EXPECTANCY,
                r.stats.trades * abs(r.stats.expectancy or 0))
    if r.strategy_id == "vwap_ema":
        cur_bars = int(get_path(data, "params.momentum_bars"))
        if cur_bars < 5:
            return ("WEAK_MOMENTUM", {"params.momentum_bars": cur_bars + 1},
                    "Require one more confirming close", ev, "Fewer whipsaw entries", METRIC_EXPECTANCY,
                    r.stats.trades * abs(r.stats.expectancy or 0))
    return None


def rule_degrading(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    if not r.degradation.degrading or r.stats.trades < min_n:
        return None
    cur = float(get_path(data, "sizing.size_multiplier"))
    new = _grid("sizing.size_multiplier", data, cur * 0.75)
    if new is None or new >= cur:
        return None
    return ("DEGRADING_REDUCE_SIZE", {"sizing.size_multiplier": new},
            f"Reduce size while the strategy degrades ({cur:g} -> {new:g})", r.degradation.reasons,
            "Smaller drawdowns while the edge is in doubt (risk-reducing only)", METRIC_DRAWDOWN,
            r.stats.trades * abs(r.stats.expectancy or 0))


def rule_slippage_otm(r: StrategyReflection, data: Mapping[str, Any], min_n: int) -> Proposal | None:
    heavy = len(r.execution.get("heavy_slippage_trades", []))
    if r.stats.trades < min_n or heavy / max(1, r.stats.trades) < 0.3:
        return None
    ev = (f"{heavy} of {r.stats.trades} trades lost a large share of their risk to slippage",)
    if get_path(data, "strike_selection.mode") == "OTM":
        return ("SLIPPAGE_OTM", {"strike_selection.mode": "ATM", "strike_selection.offset_steps": 0},
                "Trade ATM instead of OTM (tighter markets)", ev, "Lower slippage per trade", METRIC_EXPECTANCY,
                heavy * abs(r.stats.expectancy or 0))
    cur = float(get_path(data, "strike_selection.min_premium"))
    new = _grid("strike_selection.min_premium", data, cur * 1.25)
    if new is None or new <= cur:
        return None
    return ("SLIPPAGE_CHEAP_PREMIUM", {"strike_selection.min_premium": new},
            "Skip the cheapest premiums (slippage is a larger share of them)", ev, "Lower slippage share of risk",
            METRIC_EXPECTANCY, heavy * abs(r.stats.expectancy or 0))


def _ml_threshold(r: StrategyReflection, data: Mapping[str, Any], min_n: int,
                  confidences: Sequence[tuple[float, float]]) -> Proposal | None:
    """confidences: (ml_confidence, net_pnl) per trade."""
    if not get_path(data, "ml.enabled") or len(confidences) < 2 * min_n:
        return None
    cur = float(get_path(data, "ml.confidence_threshold"))
    ordered = sorted(confidences)
    low = ordered[: len(ordered) // 2]
    low_exp = sum(p for _, p in low) / len(low)
    if low_exp >= 0:
        return None
    new = _grid("ml.confidence_threshold", data, min(cur + 0.05, float(get_path(data, "ml.reduce_below") or 1.0)))
    if new is None or new <= cur:
        return None
    ev = (f"the lower-confidence half of ML-approved trades (score <= {low[-1][0]:.2f}) lost {low_exp:.0f} per "
          "trade",)
    return ("ML_THRESHOLD_TOO_LOW", {"ml.confidence_threshold": new}, f"Raise the ML threshold to {new:g}", ev,
            "Veto the weakest ML-approved setups", METRIC_EXPECTANCY, len(low) * abs(low_exp))


RULES: tuple[Rule, ...] = (rule_stops_too_tight, rule_target_too_far, rule_weak_time_window, rule_false_breakouts,
                           rule_degrading, rule_slippage_otm)


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class HypothesisGenerator:
    def __init__(self, cfg: BotConfig, *, engine: MutationEngine | None = None) -> None:
        self.cfg = cfg
        self.engine = engine or MutationEngine(cfg)
        self.learning = cfg.settings.learning

    def validation_plan(self) -> dict[str, Any]:
        g = self.learning.gates
        return {
            "gates": list(VALIDATION_GATES),
            "backtest": "full non-holdout period, candidate vs the production version, same days and costs",
            "walk_forward": f"chronological test windows (no shuffling, embargo between windows); the candidate "
                            f"must win or tie >= {g.min_fold_win_share:.0%} of >= {g.min_folds} windows",
            "out_of_sample": "final untouched holdout, evaluated once",
            "stress": "higher slippage and wider spreads; gap, high-volatility, expiry and low-liquidity days",
            "multi_seed": f"{g.bootstrap_resamples} seeded block-bootstrap resamples of daily P&L differences "
                          f"(>= {g.min_bootstrap_win_share:.0%} must favour the candidate); ML / RL models need "
                          f">= {g.min_seeds} training seeds",
            "production_comparison": f"OOS expectancy gain >= {g.min_expectancy_gain_rs:g} rupees and "
                                     f">= {g.min_expectancy_gain_pct:g}% per trade; drawdown at most "
                                     f"{g.max_drawdown_worsening_pct:g}% worse",
            "no_trade_baseline": "OOS net P&L after costs > 0 (better than not trading)",
        }

    def generate(self, reflection: StrategyReflection, *, confidences: Sequence[tuple[float, float]] = (),
                 limit: int | None = None) -> GenerationResult:
        sid = reflection.strategy_id
        scfg = self.cfg.strategies.strategies.get(sid)
        out = GenerationResult()
        if scfg is None:
            out.skipped.append(f"unknown strategy {sid}")
            return out
        data = scfg.model_dump(mode="json")
        min_n = max(3, int(math.ceil(self.learning.min_sample_trades / 10)))  # evidence floor per bucket
        proposals: list[Proposal] = []
        for rule in RULES:
            p = rule(reflection, data, min_n)
            if p is not None:
                proposals.append(p)
        p = _ml_threshold(reflection, data, min_n, confidences)
        if p is not None:
            proposals.append(p)
        # observations a parameter cannot address (strategy logic is not mutable)
        for regime, b in reflection.breakdowns.get("regime", {}).items():
            if b["trades"] >= min_n and (b["expectancy"] or 0) < 0:
                out.observations.append(f"{sid} loses in regime {regime} ({b['trades']} trades, {b['expectancy']} "
                                        "per trade): a regime filter is a logic change, outside the whitelist")
        seen: set[tuple[str, ...]] = set()
        for rule_name, change, title, ev, effect, metric, strength in sorted(proposals, key=lambda x: -x[6]):
            key = tuple(sorted(change))
            if key in seen:
                continue
            try:
                for path, value in change.items():
                    self.engine.check_value(sid, data, path, value)
            except MutationRefused as exc:
                out.skipped.append(f"{rule_name}: {exc}")
                continue
            seen.add(key)
            current = {p: get_path(data, p) for p in change}
            out.hypotheses.append(Hypothesis(
                hypothesis_id=new_uid("HYP"), rule=rule_name, strategy_id=sid, base_version=scfg.version,
                title=title, proposed_change=dict(change), current_values=current, expected_effect=effect,
                evidence=tuple(ev), test_metric=metric,
                min_sample_size=max(self.learning.min_sample_trades, 3 * min_n),
                validation_plan=self.validation_plan(),
                risk_impact=self._risk_impact(change, current),
                rollback_plan=(f"Keep {sid} {scfg.version} ready: if the candidate is deployed to PAPER and "
                               f"underperforms, the deployment controller rolls back to {scfg.version} (or the "
                               "YAML baseline) in one step; every deployment is versioned and reversible."),
                research_only=reflection.synthetic, strength=round(strength, 2)))
            if len(out.hypotheses) >= (limit or self.learning.max_hypotheses_per_strategy):
                break
        return out

    @staticmethod
    def _risk_impact(change: Mapping[str, Any], current: Mapping[str, Any]) -> str:
        parts = []
        for path, new in change.items():
            old = current.get(path)
            if path == "sizing.size_multiplier":
                parts.append(f"size multiplier {old} -> {new}: exposure per trade can only shrink")
            elif path == "stop_loss.value":
                parts.append(f"stop {old} -> {new}: a wider stop buys fewer lots for the same rupee risk; a "
                             "tighter stop buys more lots (still capped by the risk budget and limits)")
            elif path.startswith("entry_window"):
                parts.append(f"{path} {old} -> {new}: stays inside the session and before the entry cut-off")
            else:
                parts.append(f"{path} {old} -> {new}")
        return "; ".join(parts) + ". " + RISK_NOTE


__all__ = ["METRIC_DRAWDOWN", "METRIC_EXPECTANCY", "RULES", "VALIDATION_GATES", "GenerationResult", "Hypothesis",
           "HypothesisGenerator"]
