"""Performance, cost and risk-compliance metrics for a backtest.

Definitions (all on CLOSED trades, net of charges unless stated):

* total return       net P&L / allocated capital
* daily return       net P&L of trades closed that day / allocated capital,
                     for EVERY backtest day (days without trades count as 0)
* CAGR-like          compounded daily returns annualised to 252 trading days:
                     prod(1 + r_d) ** (252 / days) - 1
* Sharpe             mean(daily) / stdev(daily) * sqrt(252), risk-free 0;
                     None with fewer than 2 days or zero variance
* profit factor      sum of winning net P&L / |sum of losing net P&L|
                     (None when there are no losing trades)
* max drawdown       largest peak-to-trough fall of the closed-trade equity
                     curve (capital + cumulative net P&L), in rupees and % of
                     the peak
* average duration   entry fill -> exit fill, minutes
* cost impact        charges (brokerage, STT, exchange, SEBI, stamp, GST) plus
                     execution costs (spread + slippage vs the replayed price)
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from backtest.cost_model import CostAttribution, CostModel, Execution
from config.settings import BotConfig
from data.storage.records import TradeRecord

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class PerformanceMetrics:
    capital: float
    trading_days: int
    trades: int
    wins: int
    losses: int
    win_rate_pct: float | None
    gross_pnl: float
    charges: float
    net_pnl: float
    total_return_pct: float
    avg_daily_return_pct: float
    cagr_like_pct: float | None
    sharpe: float | None
    profit_factor: float | None
    expectancy: float | None
    avg_win: float | None
    avg_loss: float | None
    best_trade: float | None
    worst_trade: float | None
    avg_r_multiple: float | None
    max_drawdown: float
    max_drawdown_pct: float
    avg_trade_minutes: float | None
    days_with_trades: int
    exit_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def daily_returns(trades: Sequence[TradeRecord], days: Sequence[date], capital: float) -> list[float]:
    by_day: dict[date, float] = defaultdict(float)
    for t in trades:
        if t.exit_time is not None and t.net_pnl is not None:
            by_day[t.exit_time.date()] += t.net_pnl
    return [by_day.get(d, 0.0) / capital for d in days]


def max_drawdown(trades: Sequence[TradeRecord], capital: float) -> tuple[float, float]:
    equity = peak = capital
    worst, worst_pct = 0.0, 0.0
    for t in sorted(trades, key=lambda x: (x.exit_time, x.id or 0)):
        equity += t.net_pnl or 0.0
        peak = max(peak, equity)
        dd = peak - equity
        if dd > worst:
            worst, worst_pct = dd, dd / peak * 100
    return round(worst, 2), round(worst_pct, 3)


def performance(trades: Sequence[TradeRecord], days: Sequence[date], capital: float) -> PerformanceMetrics:
    closed = [t for t in trades if t.status.value == "CLOSED" and t.net_pnl is not None]
    nets = [float(t.net_pnl or 0.0) for t in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    net = sum(nets)
    rets = daily_returns(closed, days, capital)
    n_days = len(days)
    growth = math.prod(1 + r for r in rets) if rets else 1.0
    cagr = (growth ** (TRADING_DAYS_PER_YEAR / n_days) - 1) * 100 if n_days and growth > 0 else None
    sharpe = None
    if n_days >= 2:
        sd = statistics.stdev(rets)
        if sd > 0:
            sharpe = statistics.fmean(rets) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)
    durations = [(t.exit_time - t.entry_time).total_seconds() / 60 for t in closed
                 if t.exit_time is not None and t.entry_time is not None]
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    gross_loss = -sum(x for x in nets if x < 0)
    dd, dd_pct = max_drawdown(closed, capital)
    return PerformanceMetrics(
        capital=capital, trading_days=n_days, trades=len(closed), wins=len(wins), losses=len(losses),
        win_rate_pct=_r(len(wins) / len(closed) * 100) if closed else None,
        gross_pnl=round(sum(float(t.gross_pnl or 0.0) for t in closed), 2),
        charges=round(sum(t.charges for t in closed), 2), net_pnl=round(net, 2),
        total_return_pct=round(net / capital * 100, 3),
        avg_daily_return_pct=round(statistics.fmean(rets) * 100, 4) if rets else 0.0,
        cagr_like_pct=_r(cagr, 2), sharpe=_r(sharpe, 3),
        profit_factor=_r(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
        expectancy=_r(net / len(closed)) if closed else None,
        avg_win=_r(statistics.fmean(wins)) if wins else None,
        avg_loss=_r(statistics.fmean(losses)) if losses else None,
        best_trade=_r(max(nets)) if nets else None, worst_trade=_r(min(nets)) if nets else None,
        avg_r_multiple=_r(statistics.fmean(rs), 3) if rs else None, max_drawdown=dd, max_drawdown_pct=dd_pct,
        avg_trade_minutes=_r(statistics.fmean(durations), 1) if durations else None,
        days_with_trades=len({t.exit_time.date() for t in closed if t.exit_time is not None}),
        exit_reasons=dict(Counter(t.exit_reason or "UNKNOWN" for t in closed)),
    )


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CostImpact:
    attribution: CostAttribution
    gross_pnl: float  # at fill prices, before charges
    net_pnl: float
    trades: int

    @property
    def pnl_before_costs(self) -> float:
        """P&L had every fill been at the replayed price with no charges."""
        return round(self.gross_pnl + self.attribution.execution, 2)

    def as_dict(self) -> dict[str, Any]:
        a = self.attribution
        before = self.pnl_before_costs
        return {
            **a.as_dict(), "pnl_before_costs": before, "gross_pnl": round(self.gross_pnl, 2),
            "net_pnl": round(self.net_pnl, 2),
            "cost_per_trade": round(a.total / self.trades, 2) if self.trades else None,
            "cost_pct_of_turnover": round(a.total / a.turnover * 100, 3) if a.turnover else None,
            "cost_pct_of_pnl_before_costs": round(a.total / abs(before) * 100, 1) if before else None,
        }


def cost_impact(model: CostModel, executions: Sequence[Execution], trades: Sequence[TradeRecord]) -> CostImpact:
    closed = [t for t in trades if t.status.value == "CLOSED"]
    return CostImpact(model.attribute(executions), sum(float(t.gross_pnl or 0.0) for t in closed),
                      sum(float(t.net_pnl or 0.0) for t in closed), len(closed))


# --------------------------------------------------------------------------- #
# Risk compliance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComplianceCheck:
    name: str
    status: str  # OK | WARN | BREACH
    detail: str


def compliance(cfg: BotConfig, trades: Sequence[TradeRecord], *, max_open: dict[str, int], max_open_total: int,
               open_after_close: dict[str, int]) -> list[ComplianceCheck]:
    """Checks every hard risk rule against what actually happened. Any BREACH
    fails the run; WARN marks losses that overshot a limit through a gap."""
    risk = cfg.risk
    out: list[ComplianceCheck] = []

    def add(name: str, breaches: list[str], *, warn_only: bool = False) -> None:
        status = "OK" if not breaches else ("WARN" if warn_only else "BREACH")
        detail = "none" if not breaches else "; ".join(breaches[:5]) + (" ..." if len(breaches) > 5 else "")
        out.append(ComplianceCheck(name, status, detail))

    strategies = cfg.strategies.strategies

    def open_cap(sid: str) -> int:
        return min(strategies[sid].risk.max_open_trades, risk.per_strategy.max_open_trades)

    add("open trades per strategy", [f"{sid}: {n} > {open_cap(sid)}" for sid, n in max_open.items()
                                     if n > open_cap(sid)])
    add("open trades total", [f"{max_open_total} > {risk.global_.max_open_trades}"]
        if max_open_total > risk.global_.max_open_trades else [])
    per_day: Counter[tuple[str, date]] = Counter((t.strategy_id, t.created_at.date()) for t in trades)
    add("trades per strategy per day", [f"{sid} {d}: {n}" for (sid, d), n in sorted(per_day.items())
                                        if n > min(strategies[sid].risk.max_trades_per_day,
                                                   risk.per_strategy.max_trades_per_day)])
    cutoff = risk.time_rules.no_new_entries_after
    entered = [t for t in trades if t.entry_time is not None]
    add(f"no entries at/after {cutoff:%H:%M}", [f"{t.trade_uid} at {t.entry_time:%Y-%m-%d %H:%M}" for t in entered
                                                if t.entry_time is not None and t.entry_time.time() >= cutoff])
    add("entries inside the strategy entry window",
        [f"{t.trade_uid} at {t.created_at:%H:%M}" for t in trades
         if not strategies[t.strategy_id].entry_window.start <= t.created_at.time()
         < strategies[t.strategy_id].entry_window.end])
    add("flat after square-off", [f"{d}: {n} open" for d, n in sorted(open_after_close.items())])
    add("every trade had a stop-loss", [t.trade_uid for t in trades if not t.initial_stop_loss])
    late = [t for t in trades if t.exit_time is not None and t.exit_time.time() > risk.time_rules.square_off_end]
    add(f"closed by {risk.time_rules.square_off_end:%H:%M}", [t.trade_uid for t in late])

    closed = [t for t in trades if t.status.value == "CLOSED" and t.net_pnl is not None]
    over_trade = [f"{t.trade_uid}: {t.net_pnl:.0f}" for t in closed
                  if (t.net_pnl or 0.0) < -strategies[t.strategy_id].sizing.risk_per_trade]
    add("loss per trade within the per-trade risk budget (gaps can overshoot)", over_trade, warn_only=True)
    daily: dict[tuple[str, date], float] = defaultdict(float)
    for t in closed:
        assert t.exit_time is not None
        daily[(t.strategy_id, t.exit_time.date())] += t.net_pnl or 0.0
    add("daily loss per strategy within limit",
        [f"{sid} {d}: {v:.0f}" for (sid, d), v in sorted(daily.items())
         if v < -min(strategies[sid].risk.max_daily_loss, risk.per_strategy.max_daily_loss)], warn_only=True)
    glob: dict[date, float] = defaultdict(float)
    for (_, d), v in daily.items():
        glob[d] += v
    add("global daily loss within limit", [f"{d}: {v:.0f}" for d, v in sorted(glob.items())
                                           if v < -risk.global_.max_daily_loss], warn_only=True)
    return out


def compliance_status(checks: Sequence[ComplianceCheck]) -> str:
    if any(c.status == "BREACH" for c in checks):
        return "FAIL"
    return "PASS WITH WARNINGS" if any(c.status == "WARN" for c in checks) else "PASS"
