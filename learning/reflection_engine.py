"""REFLECT stage of the Reflective Loop: daily and weekly reports from the
trade journal.

``ReflectionEngine.reflect(entries, start, end)`` summarises journaled trades
per strategy (each strategy is reflected on in isolation):

* performance: trades, win rate, gross / net P&L, costs, slippage,
  expectancy (cost-adjusted, per trade), average R, profit factor, max
  drawdown, average duration
* best and worst trades
* breakdowns by regime, entry time (30-minute buckets), strike (moneyness
  at entry), stop / target / trailing configuration, exit reason and version
* execution quality: slippage share of risk, partial fills, rejected /
  unknown orders, errors
* degradation: the recent half of the window against the earlier half, and
  losing streaks
* features: Spearman rank correlation of each entry feature with the trade's
  R-multiple; PREDICTIVE / WEAK / NON_PREDICTIVE / INSUFFICIENT_DATA

Reports state which evidence they rest on. Any SYNTHETIC or UNKNOWN row makes
the report ``synthetic`` and every rendering says so on the first line; such
findings may drive research hypotheses but never a promotion.

Pure functions of the journal rows: no database writes, no trading access.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from core.clock import IST, ensure_ist
from core.enums import EvidenceSource
from data.storage.records import TradeJournalRecord

#: Entry features never analysed for predictiveness (bookkeeping / identity).
NON_FEATURES = frozenset({"as_of", "error", "trade_entry_price", "trade_stop", "trade_target", "trade_last_price",
                          "trade_open_qty", "direction", "day_of_week"})
SYNTHETIC_BANNER = ("SYNTHETIC / UNVERIFIED EVIDENCE - this reflection includes trades on generated or "
                    "unlabelled data. It is NOT a judgement of the strategy on real markets and can never "
                    "justify a promotion.")


class ReportKind(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    CUSTOM = "CUSTOM"


class FeatureVerdict(StrEnum):
    PREDICTIVE = "PREDICTIVE"
    WEAK = "WEAK"
    NON_PREDICTIVE = "NON_PREDICTIVE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class ReflectionConfig:
    min_bucket_trades: int = 3  # a breakdown bucket with fewer trades is reported but not flagged
    min_feature_samples: int = 20
    predictive_rho: float = 0.30
    non_predictive_rho: float = 0.10
    slippage_issue_pct_of_risk: float = 10.0
    degradation_min_trades: int = 6  # per half
    degradation_expectancy_drop_rs: float = 100.0
    losing_streak_alert: int = 3
    top_n: int = 3


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Stats:
    trades: int
    wins: int
    win_rate_pct: float | None
    gross_pnl: float
    costs: float
    slippage: float
    net_pnl: float
    expectancy: float | None  # net rupees per trade (after costs)
    avg_r: float | None
    profit_factor: float | None
    max_drawdown: float
    avg_minutes: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def stats(entries: Sequence[TradeJournalRecord]) -> Stats:
    nets = [e.net_pnl for e in entries]
    wins = [x for x in nets if x > 0]
    gross_loss = -sum(x for x in nets if x < 0)
    rs = [e.r_multiple for e in entries if e.r_multiple is not None]
    equity = peak = dd = 0.0
    for e in sorted(entries, key=lambda x: (x.exit_time, x.trade_uid)):
        equity += e.net_pnl
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    mins = [(e.exit_time - e.entry_time).total_seconds() / 60 for e in entries]
    n = len(entries)
    return Stats(trades=n, wins=len(wins), win_rate_pct=_r(len(wins) / n * 100) if n else None,
                 gross_pnl=round(sum(e.gross_pnl for e in entries), 2), costs=round(sum(e.costs for e in entries), 2),
                 slippage=round(sum(e.slippage for e in entries), 2), net_pnl=round(sum(nets), 2),
                 expectancy=_r(sum(nets) / n) if n else None, avg_r=_r(statistics.fmean(rs), 3) if rs else None,
                 profit_factor=_r(sum(wins) / gross_loss, 3) if gross_loss > 0 else None, max_drawdown=round(dd, 2),
                 avg_minutes=_r(statistics.fmean(mins), 1) if mins else None)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties); None if undefined."""
    if len(x) < 3 or len(x) != len(y):
        return None
    rx = pd.Series(x, dtype=float).rank().to_numpy()
    ry = pd.Series(y, dtype=float).rank().to_numpy()
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


# --------------------------------------------------------------------------- #
# Bucketing
# --------------------------------------------------------------------------- #


def time_bucket(t: datetime, minutes: int = 30) -> str:
    local = t.astimezone(IST)
    start = (local.hour * 60 + local.minute) // minutes * minutes
    end = start + minutes
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def entry_features(e: TradeJournalRecord) -> dict[str, Any]:
    """The journal row's entry features, or nothing if they were captured after
    the entry (rows from before signal-time capture): those already contain
    what the market did after the fill, so they must not be learned from."""
    as_of = e.features_entry_json.get("as_of")
    if isinstance(as_of, str) and ensure_ist(datetime.fromisoformat(as_of)) > e.entry_time:
        return {}
    return e.features_entry_json


def moneyness_bucket(e: TradeJournalRecord) -> str:
    m = entry_features(e).get("option_moneyness_pct")
    if not isinstance(m, int | float) or not math.isfinite(m):
        return "UNKNOWN"
    if m > 0.25:
        return "ITM"
    if m < -0.25:
        return "OTM"
    return "ATM"


def config_key(e: TradeJournalRecord) -> str:
    rc = e.risk_config_json
    sl, tp, tr = rc.get("stop_loss") or {}, rc.get("take_profit") or {}, rc.get("trailing") or {}
    if not sl:
        return f"v{e.strategy_version} (settings unknown)"
    trail = f"trail {tr.get('mode')} {tr.get('activation')}/{tr.get('step')}" if tr.get("enabled") else "no trail"
    return f"SL {sl.get('mode')} {sl.get('value')} | TP {tp.get('mode')} {tp.get('value')} | {trail}"


DIMENSIONS: dict[str, Callable[[TradeJournalRecord], str]] = {
    "regime": lambda e: e.regime or "UNKNOWN",
    "entry_time": lambda e: time_bucket(e.entry_time),
    "strike": moneyness_bucket,
    "option_type": lambda e: e.option_type.value,
    "stop_target_config": config_key,
    "exit_reason": lambda e: e.exit_reason or "UNKNOWN",
    "version": lambda e: e.strategy_version,
}


def breakdown(entries: Sequence[TradeJournalRecord], key: Callable[[TradeJournalRecord], str]
              ) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[TradeJournalRecord]] = defaultdict(list)
    for e in entries:
        groups[key(e)].append(e)
    return {k: stats(v).as_dict() for k, v in sorted(groups.items())}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeatureFinding:
    feature: str
    samples: int
    rho: float | None
    verdict: FeatureVerdict


@dataclass(frozen=True)
class Degradation:
    degrading: bool
    reasons: tuple[str, ...]
    earlier: dict[str, Any] | None
    recent: dict[str, Any] | None
    losing_streak: int


@dataclass
class StrategyReflection:
    strategy_id: str
    stats: Stats
    best: list[dict[str, Any]]
    worst: list[dict[str, Any]]
    breakdowns: dict[str, dict[str, dict[str, Any]]]
    execution: dict[str, Any]
    degradation: Degradation
    features: list[FeatureFinding]
    evidence: dict[str, int]  # data source -> trades
    findings: list[str] = field(default_factory=list)  # plain-language summary lines

    @property
    def synthetic(self) -> bool:
        return any(k not in (EvidenceSource.LIVE_FEED.value, EvidenceSource.HISTORICAL.value) for k in self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "stats": self.stats.as_dict(), "best": self.best,
                "worst": self.worst, "breakdowns": self.breakdowns, "execution": self.execution,
                "degradation": asdict(self.degradation), "features": [asdict(f) for f in self.features],
                "evidence": self.evidence, "synthetic": self.synthetic, "findings": self.findings}


@dataclass
class ReflectionReport:
    kind: ReportKind
    start: date
    end: date  # inclusive
    strategies: dict[str, StrategyReflection]
    generated_at: datetime

    @property
    def synthetic(self) -> bool:
        return any(s.synthetic for s in self.strategies.values())

    @property
    def evidence(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for s in self.strategies.values():
            out.update(s.evidence)
        return dict(out)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "generated_at": self.generated_at.isoformat(), "synthetic": self.synthetic,
                "evidence": self.evidence, "banner": SYNTHETIC_BANNER if self.synthetic else None,
                "strategies": {k: v.to_dict() for k, v in self.strategies.items()}}


def _trade_row(e: TradeJournalRecord) -> dict[str, Any]:
    return {"trade_uid": e.trade_uid, "net_pnl": round(e.net_pnl, 2), "r_multiple": e.r_multiple,
            "entry_time": e.entry_time.isoformat(), "exit_reason": e.exit_reason, "regime": e.regime,
            "symbol": e.trading_symbol, "version": e.strategy_version}


class ReflectionEngine:
    def __init__(self, config: ReflectionConfig | None = None, *, max_consecutive_losses: int | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self.config = config or ReflectionConfig()
        self.max_consecutive_losses = max_consecutive_losses
        self._now = now_fn or (lambda: datetime.now(IST))

    # Pieces ----------------------------------------------------------------
    def execution(self, entries: Sequence[TradeJournalRecord]) -> dict[str, Any]:
        c = self.config
        heavy = [e for e in entries
                 if (e.execution_json.get("slippage_pct_of_risk") or 0) >= c.slippage_issue_pct_of_risk]
        partial = [e for e in entries if e.execution_json.get("partial_entry") or e.execution_json.get("partial_exit")]
        failed = [e for e in entries
                  if e.execution_json.get("rejected_orders") or e.execution_json.get("unknown_orders")]
        errors = [e for e in entries if e.errors_json]
        gross_loss = -sum(e.net_pnl for e in entries if e.net_pnl < 0)
        slip = sum(e.slippage for e in entries)
        latencies = [x for e in entries if (x := e.execution_json.get("entry_latency_seconds")) is not None]
        issues = []
        if heavy:
            issues.append(f"{len(heavy)} trade(s) lost >= {c.slippage_issue_pct_of_risk:g}% of their risk to slippage")
        if failed:
            issues.append(f"{len(failed)} trade(s) had rejected / unknown orders")
        if errors:
            issues.append(f"{len(errors)} trade(s) logged errors or risk events")
        return {"slippage_rs": round(slip, 2),
                "slippage_per_trade": round(slip / len(entries), 2) if entries else None,
                "slippage_share_of_losses_pct": round(slip / gross_loss * 100, 2) if gross_loss > 0 else None,
                "heavy_slippage_trades": [e.trade_uid for e in heavy], "partial_fill_trades": len(partial),
                "failed_order_trades": len(failed), "error_trades": len(errors),
                "avg_entry_latency_seconds": round(statistics.fmean(latencies), 2) if latencies else None,
                "issues": issues}

    def degradation(self, entries: Sequence[TradeJournalRecord]) -> Degradation:
        c = self.config
        ordered = sorted(entries, key=lambda e: (e.exit_time, e.trade_uid))
        streak = best = 0
        for e in ordered:
            streak = streak + 1 if e.net_pnl <= 0 else 0
            best = max(best, streak)
        current = 0
        for e in reversed(ordered):
            if e.net_pnl > 0:
                break
            current += 1
        reasons: list[str] = []
        alert = self.max_consecutive_losses or c.losing_streak_alert
        if current >= alert:
            reasons.append(f"current losing streak of {current} trades")
        half = len(ordered) // 2
        earlier = recent = None
        if half >= c.degradation_min_trades:
            a, b = stats(ordered[:half]), stats(ordered[half:])
            earlier, recent = a.as_dict(), b.as_dict()
            if a.expectancy is not None and b.expectancy is not None \
                    and b.expectancy < 0 and a.expectancy - b.expectancy >= c.degradation_expectancy_drop_rs:
                reasons.append(f"expectancy fell from {a.expectancy:.0f} to {b.expectancy:.0f} rupees per trade")
        return Degradation(bool(reasons), tuple(reasons), earlier, recent, current)

    def features(self, entries: Sequence[TradeJournalRecord]) -> list[FeatureFinding]:
        c = self.config
        usable = [(e, entry_features(e)) for e in entries]
        names = sorted({k for _, f in usable for k in f} - NON_FEATURES)
        out: list[FeatureFinding] = []
        for name in names:
            xs, ys = [], []
            for e, f in usable:
                v = f.get(name)
                y = e.r_multiple if e.r_multiple is not None else None
                if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and y is not None:
                    xs.append(float(v))
                    ys.append(float(y))
            rho = spearman(xs, ys)
            if len(xs) < c.min_feature_samples or rho is None:
                verdict = FeatureVerdict.INSUFFICIENT_DATA
            elif abs(rho) >= c.predictive_rho:
                verdict = FeatureVerdict.PREDICTIVE
            elif abs(rho) < c.non_predictive_rho:
                verdict = FeatureVerdict.NON_PREDICTIVE
            else:
                verdict = FeatureVerdict.WEAK
            out.append(FeatureFinding(name, len(xs), _r(rho, 3), verdict))
        order = {FeatureVerdict.PREDICTIVE: 0, FeatureVerdict.WEAK: 1, FeatureVerdict.NON_PREDICTIVE: 2,
                 FeatureVerdict.INSUFFICIENT_DATA: 3}
        return sorted(out, key=lambda f: (order[f.verdict], -abs(f.rho or 0.0), f.feature))

    def _findings(self, r: StrategyReflection) -> list[str]:
        c = self.config
        lines: list[str] = []
        s = r.stats
        if s.trades == 0:
            return ["no closed trades in this window"]
        lines.append(f"{s.trades} trades, win rate {s.win_rate_pct}%, net {s.net_pnl:.0f} after "
                     f"{s.costs:.0f} costs, expectancy {s.expectancy} per trade")
        for dim in ("regime", "entry_time", "strike", "exit_reason"):
            for k, b in r.breakdowns.get(dim, {}).items():
                if b["trades"] >= c.min_bucket_trades and (b["expectancy"] or 0) < 0:
                    lines.append(f"{dim} {k}: {b['trades']} trades losing {b['expectancy']} per trade")
        lines.extend(r.execution["issues"])
        lines.extend(r.degradation.reasons)
        pred = [f.feature for f in r.features if f.verdict is FeatureVerdict.PREDICTIVE]
        if pred:
            lines.append("predictive entry features: " + ", ".join(pred[:5]))
        return lines

    # Report ----------------------------------------------------------------
    def reflect_strategy(self, strategy_id: str, entries: Sequence[TradeJournalRecord]) -> StrategyReflection:
        c = self.config
        mine = [e for e in entries if e.strategy_id == strategy_id]  # strategies are reflected on in isolation
        ranked = sorted(mine, key=lambda e: e.net_pnl)
        r = StrategyReflection(
            strategy_id=strategy_id, stats=stats(mine),
            best=[_trade_row(e) for e in reversed(ranked[-c.top_n:])] if ranked else [],
            worst=[_trade_row(e) for e in ranked[:c.top_n]],
            breakdowns={dim: breakdown(mine, fn) for dim, fn in DIMENSIONS.items()},
            execution=self.execution(mine), degradation=self.degradation(mine), features=self.features(mine),
            evidence=dict(Counter(e.data_source.value for e in mine)))
        r.findings = self._findings(r)
        return r

    def reflect(self, entries: Iterable[TradeJournalRecord], start: date, end: date, *,
                kind: ReportKind = ReportKind.CUSTOM, strategy_ids: Sequence[str] | None = None) -> ReflectionReport:
        """Trades that EXITED between ``start`` and ``end`` (inclusive, IST)."""
        if end < start:
            raise ValueError("end must not be before start")
        window = [e for e in entries if start <= e.exit_time.astimezone(IST).date() <= end]
        sids = list(strategy_ids) if strategy_ids is not None else sorted({e.strategy_id for e in window})
        return ReflectionReport(kind, start, end, {sid: self.reflect_strategy(sid, window) for sid in sids},
                                self._now())

    def daily(self, entries: Iterable[TradeJournalRecord], day: date, **kw: Any) -> ReflectionReport:
        return self.reflect(entries, day, day, kind=ReportKind.DAILY, **kw)

    def weekly(self, entries: Iterable[TradeJournalRecord], any_day: date, **kw: Any) -> ReflectionReport:
        monday = any_day - timedelta(days=any_day.weekday())
        return self.reflect(entries, monday, monday + timedelta(days=6), kind=ReportKind.WEEKLY, **kw)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _cell(v: Any) -> str:
    return "-" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))


def _table(rows: Mapping[str, Mapping[str, Any]], cols: Sequence[str]) -> list[str]:
    out = ["| bucket | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    out += [f"| {k} | " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for k, r in rows.items()]
    return out


def render_markdown(report: ReflectionReport) -> str:
    lines: list[str] = []
    if report.synthetic:
        lines += [f"> **{SYNTHETIC_BANNER}**", ""]
    lines += [f"# {report.kind.value.title()} reflection {report.start} to {report.end}", "",
              f"Generated {report.generated_at:%Y-%m-%d %H:%M} IST. Evidence: "
              + (", ".join(f"{k} {v}" for k, v in sorted(report.evidence.items())) or "no trades"), ""]
    cols = ("trades", "win_rate_pct", "net_pnl", "expectancy", "avg_r", "profit_factor")
    for sid, r in report.strategies.items():
        s = r.stats
        lines += [f"## {sid}", ""]
        lines += [f"- {x}" for x in r.findings] + [""]
        lines += ["| trades | win rate % | net | costs | slippage | expectancy | avg R | PF | max DD |",
                  "|---|---|---|---|---|---|---|---|---|",
                  f"| {s.trades} | {_cell(s.win_rate_pct)} | {_cell(s.net_pnl)} | {_cell(s.costs)} | "
                  f"{_cell(s.slippage)} | {_cell(s.expectancy)} | {_cell(s.avg_r)} | {_cell(s.profit_factor)} | "
                  f"{_cell(s.max_drawdown)} |", ""]
        if r.best:
            lines += ["Best: " + "; ".join(f"{t['trade_uid']} {t['net_pnl']:+.0f} ({t['exit_reason']})"
                                           for t in r.best),
                      "Worst: " + "; ".join(f"{t['trade_uid']} {t['net_pnl']:+.0f} ({t['exit_reason']})"
                                            for t in r.worst), ""]
        for dim, rows in r.breakdowns.items():
            if rows:
                lines += [f"### By {dim.replace('_', ' ')}", "", *_table(rows, cols), ""]
        pred = [f for f in r.features if f.verdict is not FeatureVerdict.INSUFFICIENT_DATA][:10]
        if pred:
            lines += ["### Entry features vs R-multiple (Spearman)", "", "| feature | n | rho | verdict |",
                      "|---|---|---|---|"]
            lines += [f"| {f.feature} | {f.samples} | {_cell(f.rho)} | {f.verdict.value} |" for f in pred] + [""]
    return "\n".join(lines).rstrip() + "\n"


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), tzinfo=IST)
    return start, start + timedelta(days=1)


__all__ = ["DIMENSIONS", "NON_FEATURES", "SYNTHETIC_BANNER", "Degradation", "FeatureFinding", "FeatureVerdict",
           "ReflectionConfig", "ReflectionEngine", "ReflectionReport", "ReportKind", "Stats", "StrategyReflection",
           "breakdown", "config_key", "day_bounds", "moneyness_bucket", "render_markdown", "spearman", "stats",
           "time_bucket"]
