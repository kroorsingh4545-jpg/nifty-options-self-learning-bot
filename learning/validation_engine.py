"""VERIFY stage of the Reflective Loop.

A candidate strategy version is run against the CURRENT production version
of the same strategy on the same data. Only that strategy is enabled in
either run (strategy isolation), and both go through the real PAPER bot
(``backtest.Backtester``): the same risk gate, stops, costs, kill switch
and square-off.

Runs (each a separate backtest):

* MAIN: every non-holdout day, production and candidate
* HOLDOUT: the final ``holdout_days``, untouched until this one evaluation
* STRESS: the MAIN days with extra slippage, and with wider and thinner
  quotes (low liquidity)

Gates (all must pass; the report keeps every number):

=====================  ======================================================
MIN_SAMPLE             OOS trades (walk-forward test windows + holdout) >=
                       the hypothesis' minimum sample
RISK_COMPLIANCE        no BREACH in ``backtest.metrics.compliance`` in any run
BACKTEST               MAIN: candidate expectancy (after costs) >= production
WALK_FORWARD           chronological test windows (``walk_forward_splits``,
                       embargo, never shuffled): the candidate wins or ties
                       >= ``min_fold_win_share`` of >= ``min_folds`` windows
OUT_OF_SAMPLE          HOLDOUT: candidate net >= production net
STRESS                 every stress run and day subset (gap, high-volatility,
                       expiry days): no worse than production by more than
                       ``max_stress_loss_ratio``
MULTI_SEED             ``bootstrap_resamples`` seeded resamples of the OOS daily
                       P&L differences: >= ``min_bootstrap_win_share`` favour
                       the candidate; ML / RL in use needs models trained on
                       >= ``min_seeds`` seeds
PRODUCTION_COMPARISON  the hypothesis' test metric beats production by the
                       configured margin; drawdown at most X% worse
NO_TRADE_BASELINE      OOS net P&L after costs > 0
=====================  ======================================================

``evidence_source`` is the dataset's label. Gates may pass on SYNTHETIC data
(useful to exercise the loop), but the promotion gate and the schema refuse
to promote anything not verified on HISTORICAL data.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

from backtest.backtester import (
    Backtester,
    BacktestOptions,
    BacktestResult,
    evidence_of,
    with_settings_overrides,
)
from backtest.cost_model import SpreadModel
from backtest.data import HistoricalDataset
from backtest.metrics import compliance, compliance_status
from backtest.walk_forward import walk_forward_splits
from config.settings import BotConfig, GateThresholds
from core.enums import EvidenceSource, TradeStatus
from data.storage.records import TradeRecord
from learning.hypothesis_generator import METRIC_DRAWDOWN, METRIC_EXPECTANCY, Hypothesis
from learning.mutation_engine import Candidate

GATES = ("MIN_SAMPLE", "RISK_COMPLIANCE", "BACKTEST", "WALK_FORWARD", "OUT_OF_SAMPLE", "STRESS", "MULTI_SEED",
         "PRODUCTION_COMPARISON", "NO_TRADE_BASELINE")


class ValidationError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Plan and run summaries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidationPlan:
    train_days: int = 2  # spacing before the first walk-forward test window
    test_days: int = 1
    embargo_days: int = 0
    holdout_days: int = 2
    extra_slippage_ticks: int = 2
    stress_spread: SpreadModel = field(default_factory=lambda: SpreadModel(half_spread_pct=0.75,
                                                                           min_half_spread_ticks=2, depth_lots=5))
    min_trades: int | None = None  # None: the hypothesis' minimum sample (or learning.min_sample_trades)


@dataclass(frozen=True)
class RunSummary:
    """What a gate needs from one backtest."""

    label: str
    days: tuple[date, ...]
    trades: tuple[TradeRecord, ...]
    compliance_status: str  # PASS | PASS WITH WARNINGS | FAIL
    compliance_breaches: tuple[str, ...] = ()

    def closed(self, days: Sequence[date] | None = None) -> list[TradeRecord]:
        want = set(days) if days is not None else None
        return [t for t in self.trades if t.status is TradeStatus.CLOSED and t.net_pnl is not None
                and t.exit_time is not None and (want is None or t.exit_time.date() in want)]


def metrics(trades: Sequence[TradeRecord]) -> dict[str, Any]:
    nets = [float(t.net_pnl or 0.0) for t in trades]
    equity = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x.exit_time, x.trade_uid)):
        equity += float(t.net_pnl or 0.0)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    gross_loss = -sum(x for x in nets if x < 0)
    return {"trades": len(nets), "net_pnl": round(sum(nets), 2),
            "expectancy": round(sum(nets) / len(nets), 2) if nets else None,
            "win_rate_pct": round(sum(1 for x in nets if x > 0) / len(nets) * 100, 2) if nets else None,
            "profit_factor": round(sum(x for x in nets if x > 0) / gross_loss, 3) if gross_loss > 0 else None,
            "max_drawdown": round(dd, 2), "costs": round(sum(t.charges for t in trades), 2)}


def daily_net(trades: Sequence[TradeRecord], days: Sequence[date]) -> dict[date, float]:
    out: dict[date, float] = dict.fromkeys(days, 0.0)
    for t in trades:
        if t.exit_time is not None and t.exit_time.date() in out:
            out[t.exit_time.date()] += float(t.net_pnl or 0.0)
    return out


def summarize(result: BacktestResult, strategy_id: str) -> RunSummary:
    obs = result.observations
    trades = [t for t in result.trades if t.strategy_id == strategy_id]
    checks = compliance(result.cfg, trades, max_open={k: v for k, v in obs.max_open_trades.items()
                                                      if k == strategy_id},
                        max_open_total=obs.max_open_trades_total, open_after_close=obs.open_after_close)
    return RunSummary(result.label, tuple(result.days), tuple(trades), compliance_status(checks),
                      tuple(f"{c.name}: {c.detail}" for c in checks if c.status == "BREACH"))


#: (bot config, days, scenario label) -> one strategy's run
RunFn = Callable[[BotConfig, Sequence[date], str], Awaitable[RunSummary]]


# --------------------------------------------------------------------------- #
# Day classification (stress subsets)
# --------------------------------------------------------------------------- #


def day_types(dataset: HistoricalDataset, index_key: str) -> dict[str, list[date]]:
    """Gap days and high-volatility days (top quartile of the NIFTY index), and
    weekly-expiry days (an option in the dataset expires that day)."""
    stats: dict[date, tuple[float, float]] = {}
    prev_close: float | None = None
    for d in dataset.days():
        bars = dataset.day(d).get(index_key, ())
        if not bars:
            continue
        o, c = bars[0].open, bars[-1].close
        hi, lo = max(b.high for b in bars), min(b.low for b in bars)
        gap = abs(o - prev_close) / prev_close * 100 if prev_close else 0.0
        stats[d] = (gap, (hi - lo) / o * 100 if o else 0.0)
        prev_close = c
    out: dict[str, list[date]] = {"gap": [], "high_vol": [], "expiry": []}
    if stats:
        gaps = sorted(v[0] for v in stats.values())
        rngs = sorted(v[1] for v in stats.values())
        q_gap, q_rng = gaps[int(0.75 * (len(gaps) - 1))], rngs[int(0.75 * (len(rngs) - 1))]
        out["gap"] = [d for d, v in stats.items() if v[0] >= q_gap and v[0] > 0]
        out["high_vol"] = [d for d, v in stats.items() if v[1] >= q_rng]
    expiries = {i.expiry for i in dataset.instruments if i.expiry is not None and i.option_type is not None}
    out["expiry"] = [d for d in dataset.days() if d in expiries]
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str
    numbers: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    strategy_id: str
    base_version: str
    candidate_version: str
    evidence_source: EvidenceSource
    gates: list[GateResult]
    summary: dict[str, Any]

    @property
    def passed(self) -> bool:
        return len(self.gates) == len(GATES) and all(g.passed for g in self.gates)

    @property
    def failed_gates(self) -> list[str]:
        return [g.gate for g in self.gates if not g.passed]

    def to_dict(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "base_version": self.base_version,
                "candidate_version": self.candidate_version, "evidence_source": self.evidence_source.value,
                "synthetic": self.evidence_source is not EvidenceSource.HISTORICAL, "gates_passed": self.passed,
                "failed_gates": self.failed_gates, "gates": [asdict(g) for g in self.gates],
                "summary": self.summary}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class ValidationEngine:
    def __init__(self, production: BotConfig, dataset: HistoricalDataset, *, plan: ValidationPlan | None = None,
                 options: BacktestOptions | None = None, run_fn: RunFn | None = None,
                 model_seeds: Mapping[str, int] | None = None) -> None:
        self.production = production
        self.dataset = dataset
        self.plan = plan or ValidationPlan()
        self.options = options or BacktestOptions()
        self.gates_cfg: GateThresholds = production.settings.learning.gates
        self.evidence = evidence_of(dataset.source)
        self._run_fn = run_fn
        self.model_seeds = dict(model_seeds or {})  # strategy -> seeds of the ML / RL models it runs with
        days = dataset.days()
        p = self.plan
        if len(days) < p.holdout_days + p.train_days + p.embargo_days + p.test_days:
            raise ValidationError(f"{len(days)} days cannot fit train {p.train_days} + embargo {p.embargo_days} + "
                                  f"test {p.test_days} + holdout {p.holdout_days}")
        self.wf = walk_forward_splits(days, train_days=p.train_days, test_days=p.test_days,
                                      embargo_days=p.embargo_days, holdout_days=p.holdout_days)
        self.holdout = list(self.wf.holdout)
        self.main_days = [d for d in days if d not in set(self.holdout)]
        self.oos_days = [d for f in self.wf.folds for d in f.test]
        self.types = day_types(dataset, production.settings.instruments.nifty_index_key)
        self._cache: dict[tuple[str, str, str], RunSummary] = {}

    # Runs ------------------------------------------------------------------
    async def _default_run(self, cfg: BotConfig, days: Sequence[date], scenario: str, strategy_id: str
                           ) -> RunSummary:
        opts = replace(self.options, label=scenario, database_path=None, strategies=(strategy_id,))
        if scenario.endswith("thin_quotes"):
            opts = replace(opts, spread=self.plan.stress_spread)
        if scenario.endswith("slippage"):
            ticks = cfg.settings.paper_broker.slippage_ticks + self.plan.extra_slippage_ticks
            cfg = with_settings_overrides(cfg, {"paper_broker": {"slippage_ticks": min(20, ticks)}})
        result = await Backtester(cfg, self.dataset.slice(days), opts).run()
        return summarize(result, strategy_id)

    async def _run(self, who: str, cfg: BotConfig, version: str, days: Sequence[date], scenario: str,
                   strategy_id: str) -> RunSummary:
        key = (f"{strategy_id}:{version}", scenario, ",".join(d.isoformat() for d in days))
        if key not in self._cache:
            if self._run_fn is not None:
                self._cache[key] = await self._run_fn(cfg, days, f"{who}:{scenario}")
            else:
                self._cache[key] = await self._default_run(cfg, days, f"{who}:{scenario}", strategy_id)
        return self._cache[key]

    # Verify ----------------------------------------------------------------
    async def verify(self, candidate: Candidate, hypothesis: Hypothesis | None = None) -> ValidationReport:
        sid = candidate.strategy_id
        prod_scfg = self.production.strategies.strategies.get(sid)
        if prod_scfg is None:
            raise ValidationError(f"unknown strategy {sid!r}")
        if prod_scfg.version != candidate.base_version:
            raise ValidationError(f"{sid}: candidate {candidate.version} was built on {candidate.base_version}, "
                                  f"production is {prod_scfg.version}: re-derive it from production")
        g = self.gates_cfg
        pv, cv = prod_scfg.version, candidate.version
        runs: dict[str, tuple[RunSummary, RunSummary]] = {}
        for scenario, days in (("main", self.main_days), ("holdout", self.holdout),
                               ("stress_slippage", self.main_days), ("stress_thin_quotes", self.main_days)):
            prod = await self._run("production", self.production, pv, days, scenario, sid)
            cand = await self._run("candidate", candidate.bot_config, cv, days, scenario, sid)
            runs[scenario] = (prod, cand)
        prod_main, cand_main = runs["main"]
        prod_hold, cand_hold = runs["holdout"]
        oos_cand = cand_main.closed(self.oos_days) + cand_hold.closed()
        oos_prod = prod_main.closed(self.oos_days) + prod_hold.closed()
        oos_days = self.oos_days + self.holdout
        m_cand, m_prod = metrics(oos_cand), metrics(oos_prod)
        gates: list[GateResult] = []

        # MIN_SAMPLE
        need = self.plan.min_trades or (hypothesis.min_sample_size if hypothesis else
                                        self.production.settings.learning.min_sample_trades)
        gates.append(GateResult("MIN_SAMPLE", m_cand["trades"] >= need,
                                f"{m_cand['trades']} OOS trades (need {need})",
                                {"oos_trades": m_cand["trades"], "required": need}))
        # RISK_COMPLIANCE
        bad = {name: cand.compliance_breaches for name, (_, cand) in runs.items()
               if cand.compliance_status == "FAIL"}
        gates.append(GateResult("RISK_COMPLIANCE", not bad, "no risk-limit breach in any candidate run" if not bad
                                else f"breaches: {bad}", {"breaches": {k: list(v) for k, v in bad.items()}}))
        # BACKTEST
        mc, mp = metrics(cand_main.closed()), metrics(prod_main.closed())
        ok = mc["expectancy"] is not None and (mp["expectancy"] is None or mc["expectancy"] >= mp["expectancy"])
        gates.append(GateResult("BACKTEST", ok, f"MAIN expectancy {mc['expectancy']} vs production "
                                f"{mp['expectancy']}", {"candidate": mc, "production": mp}))
        # WALK_FORWARD
        folds = []
        for f in self.wf.folds:
            c_net = sum(float(t.net_pnl or 0) for t in cand_main.closed(f.test))
            p_net = sum(float(t.net_pnl or 0) for t in prod_main.closed(f.test))
            folds.append({"fold": f.index, "test": [d.isoformat() for d in f.test], "candidate": round(c_net, 2),
                          "production": round(p_net, 2), "won_or_tied": c_net >= p_net})
        share = sum(1 for f in folds if f["won_or_tied"]) / len(folds) if folds else 0.0
        ok = len(folds) >= g.min_folds and share >= g.min_fold_win_share
        gates.append(GateResult("WALK_FORWARD", ok, f"won or tied {share:.0%} of {len(folds)} test windows "
                                f"(need {g.min_fold_win_share:.0%} of >= {g.min_folds})",
                                {"folds": folds, "win_share": round(share, 3)}))
        # OUT_OF_SAMPLE
        hc, hp = metrics(cand_hold.closed()), metrics(prod_hold.closed())
        gates.append(GateResult("OUT_OF_SAMPLE", hc["net_pnl"] >= hp["net_pnl"],
                                f"holdout net {hc['net_pnl']} vs production {hp['net_pnl']} "
                                f"({len(self.holdout)} untouched days, evaluated once)",
                                {"candidate": hc, "production": hp, "days": [d.isoformat() for d in self.holdout]}))
        # STRESS
        scenarios: dict[str, tuple[float, float]] = {}
        for name in ("stress_slippage", "stress_thin_quotes"):
            p, c = runs[name]
            scenarios[name] = (metrics(c.closed())["net_pnl"], metrics(p.closed())["net_pnl"])
        for kind, days in self.types.items():
            in_main = [d for d in days if d in set(self.main_days)]
            if in_main:
                scenarios[f"{kind}_days"] = (metrics(cand_main.closed(in_main))["net_pnl"],
                                             metrics(prod_main.closed(in_main))["net_pnl"])
        worse = {k: v for k, v in scenarios.items()
                 if v[0] < (v[1] if v[1] >= 0 else v[1] * g.max_stress_loss_ratio)}
        gates.append(GateResult("STRESS", not worse, "no stress scenario worse than allowed" if not worse else
                                f"worse than production beyond the limit: {sorted(worse)}",
                                {"scenarios": {k: {"candidate": v[0], "production": v[1]}
                                               for k, v in scenarios.items()}}))
        # MULTI_SEED
        dc, dp = daily_net(oos_cand, oos_days), daily_net(oos_prod, oos_days)
        diffs = [dc[d] - dp[d] for d in oos_days]
        wins = 0
        for seed in range(g.bootstrap_resamples):
            rng = random.Random(seed)  # noqa: S311 - statistical resampling, not cryptography
            sample = [diffs[rng.randrange(len(diffs))] for _ in diffs] if diffs else [0.0]
            wins += statistics.fmean(sample) > 0
        boot = wins / g.bootstrap_resamples
        scfg = candidate.config
        uses_models = scfg.ml.enabled or scfg.rl.enabled
        seeds = self.model_seeds.get(sid, 0)
        seeds_ok = not uses_models or seeds >= g.min_seeds
        ok = boot >= g.min_bootstrap_win_share and seeds_ok
        gates.append(GateResult("MULTI_SEED", ok, f"{boot:.0%} of {g.bootstrap_resamples} seeded resamples favour "
                                f"the candidate (need {g.min_bootstrap_win_share:.0%})"
                                + ("" if seeds_ok else f"; ML/RL models trained on {seeds} seeds "
                                                       f"(need {g.min_seeds})"),
                                {"bootstrap_win_share": round(boot, 3), "resamples": g.bootstrap_resamples,
                                 "uses_models": uses_models, "model_seeds": seeds}))
        # PRODUCTION_COMPARISON
        metric = hypothesis.test_metric if hypothesis else METRIC_EXPECTANCY
        ce, pe = m_cand["expectancy"], m_prod["expectancy"]
        dd_cap = m_prod["max_drawdown"] * (1 + g.max_drawdown_worsening_pct / 100)
        dd_ok = m_cand["max_drawdown"] <= max(dd_cap, m_prod["max_drawdown"] + 1e-9)
        if ce is None:
            ok, detail = False, "no OOS trades"
        elif metric == METRIC_DRAWDOWN:
            ok = m_cand["max_drawdown"] < m_prod["max_drawdown"] and (pe is None or ce >= pe - g.min_expectancy_gain_rs)
            detail = (f"OOS drawdown {m_cand['max_drawdown']} vs production {m_prod['max_drawdown']}; expectancy "
                      f"{ce} vs {pe}")
        else:
            need_gain = max(g.min_expectancy_gain_rs, g.min_expectancy_gain_pct / 100 * abs(pe or 0.0))
            ok = (pe is None or ce - pe >= need_gain) and dd_ok
            detail = (f"OOS expectancy {ce} vs production {pe} (need +{need_gain:.0f}); drawdown "
                      f"{m_cand['max_drawdown']} vs {m_prod['max_drawdown']}")
        gates.append(GateResult("PRODUCTION_COMPARISON", ok, detail,
                                {"metric": metric, "candidate": m_cand, "production": m_prod}))
        # NO_TRADE_BASELINE
        gates.append(GateResult("NO_TRADE_BASELINE", m_cand["net_pnl"] > 0,
                                f"OOS net after costs {m_cand['net_pnl']} vs 0 for not trading",
                                {"oos_net": m_cand["net_pnl"]}))
        summary = {"oos_days": [d.isoformat() for d in oos_days], "main_days": len(self.main_days),
                   "holdout_days": len(self.holdout), "candidate_oos": m_cand, "production_oos": m_prod,
                   "changes": [m.as_dict() for m in candidate.mutations], "day_types":
                   {k: [d.isoformat() for d in v] for k, v in self.types.items()},
                   "data_source": self.dataset.source, "data_description": self.dataset.description}
        return ValidationReport(sid, pv, cv, self.evidence, gates, summary)


__all__ = ["GATES", "GateResult", "RunFn", "RunSummary", "ValidationEngine", "ValidationError", "ValidationPlan",
           "ValidationReport", "daily_net", "day_types", "metrics", "summarize"]
