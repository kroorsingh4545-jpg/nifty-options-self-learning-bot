"""Backtest reports: JSON (machine-readable), Markdown (human) and trades CSV.

Report sections:
 1. header        label, data source (SYNTHETIC data is flagged loudly), period
 2. summary       overall and per-strategy metrics (``backtest.metrics``)
 3. cost impact   charges by component, spread, slippage, P&L before costs
 4. compliance    every hard risk rule checked against what happened
 5. pipeline      signals, approvals and the most common rejection reasons
 6. daily         net P&L per day and strategy
 7. trades        every trade (also in trades.csv)
 8. assumptions   fill model, cost rates, spread model, limitations
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from backtest.backtester import TICK_OFFSETS_SECONDS, BacktestResult
from backtest.metrics import compliance, compliance_status, cost_impact, performance
from backtest.walk_forward import WalkForwardResult
from core.clock import now_ist

SYNTHETIC_BANNER = ("SYNTHETIC DATA - generated random-walk prices, NOT real NIFTY data. These results say nothing "
                    "about how the strategies would perform in the market.")

LIMITATIONS = (
    "Fills are simulated by the paper broker against prices replayed from 1-minute candles (4 ticks per candle); "
    "real order books, queue position and latency are not modelled.",
    "Option bid/ask are synthesised by the spread model; historical quotes are not used.",
    "Within a candle the path is open -> low -> high -> close (up-candle) or open -> high -> low -> close "
    "(down-candle); the true intra-minute order is unknown.",
    "Each run starts cold: indicator warm-up (e.g. the VWAP+EMA strategy's EMA) is rebuilt from the first day.",
    "Charges use the configured rates in config/settings.yaml; verify them against current Upstox/NSE sheets.",
)


@dataclass
class BacktestReport:
    label: str
    generated_at: str
    data_source: str
    data_description: str
    synthetic: bool
    first_day: str
    last_day: str
    trading_days: int
    strategies: list[str]
    config_fingerprint: str
    summary: dict[str, Any]
    by_strategy: dict[str, dict[str, Any]]
    costs: dict[str, Any]
    costs_by_strategy: dict[str, dict[str, Any]]
    compliance_status: str
    compliance: list[dict[str, str]]
    pipeline: dict[str, Any]
    daily: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    assumptions: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    risk_events: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def config_fingerprint(result: BacktestResult) -> str:
    cfg = result.cfg
    blob = json.dumps({"strategies": cfg.strategies.model_dump(mode="json"), "risk": cfg.risk.model_dump(mode="json"),
                       "costs": cfg.settings.costs.model_dump(mode="json"),
                       "paper_broker": cfg.settings.paper_broker.model_dump(mode="json"),
                       "spread": result.cost_model.spread.model_dump(mode="json")}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


TRADE_COLUMNS = ("trade_uid", "strategy_id", "strategy_version", "symbol", "option_type", "strike", "expiry",
                 "quantity", "status", "created_at", "entry_time", "entry_price", "initial_stop_loss", "target",
                 "exit_time", "exit_price", "exit_reason", "gross_pnl", "charges", "net_pnl", "r_multiple",
                 "entry_reason")


def _trade_row(t: Any) -> dict[str, Any]:
    return {"trade_uid": t.trade_uid, "strategy_id": t.strategy_id, "strategy_version": t.strategy_version,
            "symbol": t.trading_symbol, "option_type": t.option_type.value, "strike": t.strike,
            "expiry": t.expiry.isoformat(), "quantity": t.quantity, "status": t.status.value,
            "created_at": t.created_at.isoformat(), "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "entry_price": t.entry_price, "initial_stop_loss": t.initial_stop_loss, "target": t.target,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None, "exit_price": t.exit_price,
            "exit_reason": t.exit_reason, "gross_pnl": t.gross_pnl, "charges": t.charges, "net_pnl": t.net_pnl,
            "r_multiple": t.r_multiple, "entry_reason": t.entry_reason}


def build_report(result: BacktestResult, *, generated_at: datetime | None = None) -> BacktestReport:
    """``generated_at`` is when the report was written (wall clock); every
    trade/order timestamp inside the report is simulated time."""
    days = result.days
    total_capital = sum(result.capital.values()) or result.total_capital
    summary = performance(result.trades, days, total_capital).as_dict()
    by_strategy = {sid: performance([t for t in result.trades if t.strategy_id == sid], days, cap).as_dict()
                   for sid, cap in result.capital.items()}
    costs = cost_impact(result.cost_model, result.executions, result.trades).as_dict()
    costs_by = {sid: cost_impact(result.cost_model, [e for e in result.executions if e.strategy_id == sid],
                                 [t for t in result.trades if t.strategy_id == sid]).as_dict()
                for sid in result.capital}
    obs = result.observations
    checks = compliance(result.cfg, result.trades, max_open=obs.max_open_trades,
                        max_open_total=obs.max_open_trades_total, open_after_close=obs.open_after_close)
    daily: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(result.capital, 0.0))
    for d in days:
        _ = daily[d.isoformat()]
    for t in result.closed_trades():
        if t.exit_time is not None and t.strategy_id in result.capital:
            daily[t.exit_time.date().isoformat()][t.strategy_id] += t.net_pnl or 0.0
    daily_rows = [{"day": d, **{k: round(v, 2) for k, v in row.items()}, "total": round(sum(row.values()), 2)}
                  for d, row in sorted(daily.items())]
    pipeline = {sid: {"signals": obs.signals.get(sid, 0), "approved": obs.approved.get(sid, 0),
                      "top_rejections": obs.rejections[sid].most_common(6) if sid in obs.rejections else []}
                for sid in result.capital}
    risk_events: dict[str, int] = defaultdict(int)
    for e in result.risk_events:
        risk_events[e.event_type] += 1
    pb = result.cfg.settings.paper_broker
    assumptions = {
        "fill_model": {"engine": "PaperBroker (same code as PAPER trading)",
                       "ticks_per_candle": len(TICK_OFFSETS_SECONDS),
                       "tick_offsets_seconds": list(TICK_OFFSETS_SECONDS),
                       "intra_candle_path": "up: O-L-H-C, down: O-H-L-C",
                       "entry": "LIMIT at ask + entry_limit_buffer_ticks", "exit": "MARKET at bid",
                       "protective_stop": "resting SL-M at the stop (exchange-side)",
                       "partial_fills": pb.simulate_partial_fills, "max_quote_age_seconds": pb.max_quote_age_seconds},
        "costs": result.cost_model.describe(),
        "capital": {"per_strategy": result.capital, "total": total_capital},
        "limitations": list(LIMITATIONS),
    }
    return BacktestReport(
        label=result.label, generated_at=(generated_at or now_ist()).isoformat(timespec="seconds"),
        data_source=result.data_source, data_description=result.data_description,
        synthetic=result.data_source == "SYNTHETIC", first_day=days[0].isoformat(), last_day=days[-1].isoformat(),
        trading_days=len(days), strategies=list(result.capital), config_fingerprint=config_fingerprint(result),
        summary=summary, by_strategy=by_strategy, costs=costs, costs_by_strategy=costs_by,
        compliance_status=compliance_status(checks), compliance=[asdict(c) for c in checks], pipeline=pipeline,
        daily=daily_rows, trades=[_trade_row(t) for t in result.trades], assumptions=assumptions,
        warnings=list(result.warnings), risk_events=dict(risk_events), elapsed_seconds=result.elapsed_seconds,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v}{suffix}"


METRIC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Capital (₹)", "capital", ""), ("Trades", "trades", ""), ("Win rate", "win_rate_pct", " %"),
    ("Net P&L (₹)", "net_pnl", ""), ("Gross P&L (₹)", "gross_pnl", ""), ("Charges (₹)", "charges", ""),
    ("Total return", "total_return_pct", " %"), ("Avg daily return", "avg_daily_return_pct", " %"),
    ("CAGR-like (252 d)", "cagr_like_pct", " %"), ("Sharpe (daily, ann.)", "sharpe", ""),
    ("Profit factor", "profit_factor", ""), ("Expectancy / trade (₹)", "expectancy", ""),
    ("Avg win (₹)", "avg_win", ""), ("Avg loss (₹)", "avg_loss", ""), ("Avg R-multiple", "avg_r_multiple", ""),
    ("Max drawdown (₹)", "max_drawdown", ""), ("Max drawdown", "max_drawdown_pct", " %"),
    ("Avg trade duration (min)", "avg_trade_minutes", ""), ("Days with trades", "days_with_trades", ""),
)

COST_ROWS: tuple[tuple[str, str], ...] = (
    ("P&L before costs (at replayed price)", "pnl_before_costs"), ("Spread", "spread"), ("Slippage", "slippage"),
    ("Brokerage", "brokerage"), ("STT", "stt"), ("Exchange txn", "exchange_txn"), ("SEBI fee", "sebi_fee"),
    ("Stamp duty", "stamp_duty"), ("GST", "gst"), ("Total costs", "total"), ("Net P&L", "net_pnl"),
    ("Cost per trade", "cost_per_trade"), ("Costs % of turnover", "cost_pct_of_turnover"),
    ("Costs % of P&L before costs", "cost_pct_of_pnl_before_costs"),
)


def render_markdown(r: BacktestReport, *, max_trades: int = 200) -> str:
    out: list[str] = [f"# Backtest report — {r.label}", ""]
    if r.synthetic:
        out += [f"> **⚠ {SYNTHETIC_BANNER}**", ""]
    out += [f"- Data: **{r.data_source}** — {r.data_description or 'n/a'}",
            f"- Period: {r.first_day} → {r.last_day} ({r.trading_days} trading days)",
            f"- Strategies: {', '.join(r.strategies)}",
            f"- Risk compliance: **{r.compliance_status}**",
            f"- Config fingerprint: `{r.config_fingerprint}` · generated {r.generated_at} · "
            f"run time {r.elapsed_seconds:.1f} s", ""]
    sids = list(r.by_strategy)
    out += ["## Summary", "", "| Metric | Total | " + " | ".join(sids) + " |",
            "|---|---:|" + "---:|" * len(sids)]
    for label, key, suffix in METRIC_ROWS:
        cells = [_fmt(r.summary.get(key), suffix)] + [_fmt(r.by_strategy[s].get(key), suffix) for s in sids]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    reasons = {k for m in [r.summary, *r.by_strategy.values()] for k in m.get("exit_reasons", {})}
    for reason in sorted(reasons):
        cells = [str(r.summary["exit_reasons"].get(reason, 0))] + \
                [str(r.by_strategy[s]["exit_reasons"].get(reason, 0)) for s in sids]
        out.append(f"| Exits: {reason} | " + " | ".join(cells) + " |")
    out += ["", "## Cost impact (₹)", "", "| Item | Total | " + " | ".join(sids) + " |",
            "|---|---:|" + "---:|" * len(sids)]
    for label, key in COST_ROWS:
        cells = [_fmt(r.costs.get(key))] + [_fmt(r.costs_by_strategy[s].get(key)) for s in sids]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out += ["", f"## Risk compliance: {r.compliance_status}", "", "| Check | Status | Detail |", "|---|---|---|"]
    out += [f"| {c['name']} | {c['status']} | {c['detail']} |" for c in r.compliance]
    if r.risk_events:
        out += ["", "Risk events: " + ", ".join(f"{k} × {v}" for k, v in sorted(r.risk_events.items()))]
    out += ["", "## Signal pipeline", "", "| Strategy | Signals | Approved | Most common rejections |",
            "|---|---:|---:|---|"]
    for sid, p in r.pipeline.items():
        rej = "; ".join(f"{reason} ({n})" for reason, n in p["top_rejections"]) or "—"
        out.append(f"| {sid} | {p['signals']} | {p['approved']} | {rej} |")
    out += ["", "## Daily net P&L (₹)", "", "| Day | " + " | ".join(sids) + " | Total |",
            "|---|" + "---:|" * (len(sids) + 1)]
    for row in r.daily:
        out.append(f"| {row['day']} | " + " | ".join(_fmt(row[s]) for s in sids) + f" | {_fmt(row['total'])} |")
    out += ["", f"## Trades ({len(r.trades)})", "",
            "| Strategy | Contract | Qty | Entry | Exit | Entry ₹ | Exit ₹ | Reason | Net ₹ | R |",
            "|---|---|---:|---|---|---:|---:|---|---:|---:|"]
    for t in r.trades[:max_trades]:
        out.append(f"| {t['strategy_id']} | {t['symbol']} | {t['quantity']} | {(t['entry_time'] or '')[:16]} | "
                   f"{(t['exit_time'] or '')[11:16]} | {_fmt(t['entry_price'])} | {_fmt(t['exit_price'])} | "
                   f"{t['exit_reason'] or t['status']} | {_fmt(t['net_pnl'])} | {_fmt(t['r_multiple'])} |")
    if len(r.trades) > max_trades:
        out.append(f"| … {len(r.trades) - max_trades} more in trades.csv | | | | | | | | | |")
    a = r.assumptions
    fm = a["fill_model"]
    out += ["", "## Assumptions", "",
            f"- Fills: {fm['engine']}; {fm['ticks_per_candle']} ticks per 1-minute candle "
            f"({fm['intra_candle_path']}); entry {fm['entry']}; exit {fm['exit']}; "
            f"protective stop {fm['protective_stop']}.",
            f"- Costs: {json.dumps(a['costs']['rates'])}; slippage {a['costs']['slippage_ticks']} tick(s); "
            f"spread {json.dumps(a['costs']['spread'])}; brokerage {a['costs']['brokerage_basis']}.",
            f"- Capital: {json.dumps(a['capital']['per_strategy'])} (total ₹{a['capital']['total']:,.0f})."]
    out += [f"- Limitation: {x}" for x in a["limitations"]]
    if r.warnings:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in r.warnings]
    return "\n".join(out) + "\n"


def render_walk_forward_markdown(wf: WalkForwardResult) -> str:
    first = wf.folds[0].test if wf.folds else None
    synthetic = first is not None and first.data_source == "SYNTHETIC"
    out = ["# Walk-forward validation", ""]
    if synthetic:
        out += [f"> **⚠ {SYNTHETIC_BANNER}**", ""]
    p = wf.plan
    out += [f"- Folds: {len(wf.folds)} ({'anchored' if p.anchored else 'rolling'} train), embargo {p.embargo_days} "
            f"day(s), holdout {len(p.holdout)} day(s)"
            + (f" ({p.holdout[0]} → {p.holdout[-1]}, untouched)" if p.holdout and wf.holdout is None else ""),
            f"- Candidates: {len(wf.candidates)} — " + "; ".join(
                f"c{i}={json.dumps(c, default=str) if c else 'current config'}" for i, c in enumerate(wf.candidates)),
            "", "| Fold | Train | Test | Chosen | Train scores | Test trades | Test net ₹ | Test PF |",
            "|---:|---|---|---:|---|---:|---:|---:|"]
    for f in wf.folds:
        m = performance(f.test.trades, list(f.fold.test), sum(f.test.capital.values()))
        scores = ", ".join(f"{s:,.0f}" for s in f.train_scores) or "—"
        out.append(f"| {f.fold.index} | {f.fold.train[0]} → {f.fold.train[-1]} | {f.fold.test[0]} → "
                   f"{f.fold.test[-1]} | c{f.chosen} | {scores} | {m.trades} | {m.net_pnl:,.2f} | "
                   f"{_fmt(m.profit_factor)} |")
    o = wf.out_of_sample
    out += ["", "## Out-of-sample (all test windows)", "", "| Metric | Value |", "|---|---:|"]
    out += [f"| {label} | {_fmt(getattr(o, key), suffix)} |" for label, key, suffix in METRIC_ROWS]
    if wf.holdout is not None:
        h = performance(wf.holdout.trades, wf.holdout.days, sum(wf.holdout.capital.values()))
        out += ["", "## Final holdout", "", f"Trades {h.trades}, net ₹{h.net_pnl:,.2f}, "
                f"profit factor {_fmt(h.profit_factor)}, max drawdown ₹{h.max_drawdown:,.2f}"]
    if wf.notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in wf.notes]
    return "\n".join(out) + "\n"


def write_report(report: BacktestReport, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / "report.json", "markdown": out_dir / "report.md", "trades": out_dir / "trades.csv"}
    paths["json"].write_text(report.to_json(), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    with paths["trades"].open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        w.writeheader()
        w.writerows(report.trades)
    return paths
