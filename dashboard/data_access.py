"""Read-only view models for the dashboard.

Sources:
* the bot's published state snapshot (``runtime_state``; live values such as
  LTP, unrealized P&L, kill-switch and connection state), and
* the SQLite history (trades, orders, risk events, commands, model runs,
  learning events).

No trading or risk decisions are made here: only presentation arithmetic
(win rate, profit factor, drawdown, chart overlays). Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from config.settings import BotConfig
from core.clock import ensure_ist, now_ist
from core.enums import ModelRunStatus, TradeStatus
from data.storage.database import Database
from data.storage.records import TradeRecord
from data.storage.repositories import Repositories
from strategies.indicators import EMA, SessionVWAP

STALE_AFTER_SECONDS = 5.0


def database_path(cfg: BotConfig, override: str | None = None) -> Path:
    path = Path(override) if override else cfg.settings.database.path
    return path if path.is_absolute() else cfg.project_root / path


def open_repositories(cfg: BotConfig, path: Path) -> Repositories:
    """Open the bot's database. The dashboard never creates or migrates the
    schema (only the bot does): a missing file or any schema-version
    mismatch raises ``DatabaseError``."""
    return Repositories(Database.open_existing(path), cfg.mode)


@dataclass(frozen=True)
class RuntimeView:
    state: dict[str, Any] | None
    age_seconds: float | None

    @property
    def online(self) -> bool:
        return self.state is not None and self.age_seconds is not None and self.age_seconds <= STALE_AFTER_SECONDS

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.state
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def runtime_view(repos: Repositories, now: datetime | None = None) -> RuntimeView:
    latest = repos.runtime_state.latest()
    if latest is None:
        return RuntimeView(None, None)
    state, published = latest
    age = ((now or now_ist()) - published).total_seconds()
    return RuntimeView(state, max(0.0, age))


# --------------------------------------------------------------------------- #
# Trades and performance
# --------------------------------------------------------------------------- #


def all_trades(repos: Repositories, strategies: Sequence[str]) -> list[TradeRecord]:
    out: list[TradeRecord] = []
    for sid in strategies:
        out.extend(repos.trades.list_for_strategy(sid))
    return sorted(out, key=lambda t: t.exit_time or t.entry_time or t.created_at)


@dataclass(frozen=True)
class Performance:
    closed: int
    wins: int
    losses: int
    win_rate: float | None
    profit_factor: float | None
    net_pnl: float
    max_drawdown: float
    equity: pd.DataFrame  # columns: time, strategy_id, net_pnl, cumulative, drawdown
    by_strategy: dict[str, float]


def performance(trades: Sequence[TradeRecord]) -> Performance:
    closed = [t for t in trades if t.status is TradeStatus.CLOSED and t.net_pnl is not None]
    closed.sort(key=lambda t: t.exit_time or t.created_at)
    pnls = [float(t.net_pnl or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    rows = []
    cum = peak = 0.0
    max_dd = 0.0
    for t, p in zip(closed, pnls, strict=True):
        cum += p
        peak = max(peak, cum)
        dd = cum - peak
        max_dd = min(max_dd, dd)
        rows.append({"time": t.exit_time or t.created_at, "strategy_id": t.strategy_id, "net_pnl": p,
                     "cumulative": cum, "drawdown": dd})
    by_strategy: dict[str, float] = {}
    for t, p in zip(closed, pnls, strict=True):
        by_strategy[t.strategy_id] = by_strategy.get(t.strategy_id, 0.0) + p
    return Performance(
        closed=len(closed), wins=len(wins), losses=len(losses),
        win_rate=(len(wins) / len(closed)) if closed else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        net_pnl=sum(pnls), max_drawdown=max_dd,
        equity=pd.DataFrame(rows, columns=["time", "strategy_id", "net_pnl", "cumulative", "drawdown"]),
        by_strategy=by_strategy,
    )


def trades_on(trades: Sequence[TradeRecord], day: date) -> list[TradeRecord]:
    return [t for t in trades if t.created_at.date() == day]


# --------------------------------------------------------------------------- #
# Portfolio overview
# --------------------------------------------------------------------------- #


def portfolio(cfg: BotConfig, view: RuntimeView, perf: Performance, trades_today: int) -> dict[str, Any]:
    daily = view.get("daily", default={}) or {}
    strategies = (daily.get("strategies") or {}).values()
    realized = sum(float(s.get("realized", 0.0)) for s in strategies)
    unrealized = sum(float(s.get("unrealized", 0.0)) for s in strategies)
    global_limit = float(daily.get("global_limit") or cfg.risk.global_.max_daily_loss)
    global_loss = float(daily.get("global_loss") or 0.0)
    funds = view.get("funds") or {}
    return {
        "total_capital": cfg.risk.capital.total_capital,
        "deployed_capital": view.get("capital", "deployed", default=0.0),
        "available_funds": funds.get("available_margin"),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": realized + unrealized,
        "daily_loss_used": global_loss,
        "daily_loss_remaining": max(0.0, global_limit - global_loss),
        "daily_loss_limit": global_limit,
        "open_positions": len(view.get("positions", default=[]) or []),
        "trades_today": trades_today,
        "win_rate": perf.win_rate,
        "profit_factor": perf.profit_factor,
        "max_drawdown": perf.max_drawdown,
    }


# --------------------------------------------------------------------------- #
# Chart data
# --------------------------------------------------------------------------- #


def candles_frame(view: RuntimeView, key: str | None, tf: int) -> pd.DataFrame:
    rows = (view.get("candles", default={}) or {}).get(f"{key}|{tf}", []) if key else []
    df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df["start"] = pd.to_datetime(df["start"])
    return df


def ema_series(closes: Sequence[float], period: int) -> list[float | None]:
    ema = EMA(period)
    out: list[float | None] = []
    for c in closes:
        v = ema.update(float(c))
        out.append(v if ema.ready else None)
    return out


def vwap_series(df: pd.DataFrame) -> list[float | None]:
    vwap = SessionVWAP()
    out: list[float | None] = []
    for start, high, low, close, volume in zip(df["start"].tolist(), df["high"].tolist(), df["low"].tolist(),
                                               df["close"].tolist(), df["volume"].tolist(), strict=True):
        out.append(vwap.update(start.date(), float(high), float(low), float(close), float(volume)))
    return out


def trade_markers(trades: Sequence[TradeRecord], candles: pd.DataFrame) -> pd.DataFrame:
    """Entries and exits placed on the NIFTY chart at the candle close nearest
    to each event time (the trades themselves are option contracts). Events
    outside the plotted candle window are left out rather than stretching
    the time axis."""
    cols = ["time", "price", "kind", "label"]
    if candles.empty:
        return pd.DataFrame(columns=cols)
    times: list[Any] = candles["start"].tolist()
    closes: list[float] = [float(c) for c in candles["close"].tolist()]
    step = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else pd.Timedelta(minutes=5)
    lo, hi = times[0] - step, times[-1] + 2 * step
    out = []
    for t in trades:
        for when, kind in ((t.entry_time, "entry"), (t.exit_time, "exit")):
            if when is None:
                continue
            ts = pd.Timestamp(ensure_ist(when))
            if not lo <= ts <= hi:
                continue
            i = min(range(len(times)), key=lambda j: abs((times[j] - ts).total_seconds()))
            out.append({"time": ts, "price": closes[i], "kind": kind,
                        "label": f"{kind} {t.strategy_id} {t.trading_symbol}"})
    return pd.DataFrame(out, columns=cols)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def orders_table(repos: Repositories, limit: int = 100) -> pd.DataFrame:
    rows = repos.db.query(
        "SELECT created_at, strategy_id, trading_symbol, side, order_type, purpose, quantity, price, trigger_price, "
        "status, filled_quantity, average_price, status_message FROM orders WHERE mode = ? ORDER BY id DESC LIMIT ?",
        (repos.mode.value, int(limit)))
    return pd.DataFrame([dict(r) for r in rows])


def risk_events_table(repos: Repositories, limit: int = 100) -> pd.DataFrame:
    return pd.DataFrame([{"time": e.created_at, "severity": e.severity.value, "strategy": e.strategy_id or "GLOBAL",
                          "event": e.event_type, "message": e.message} for e in repos.risk_events.recent(limit)])


def commands_table(repos: Repositories, limit: int = 50) -> pd.DataFrame:
    return pd.DataFrame([{"#": c.id, "requested": c.requested_at, "command": c.command.value, "operator": c.operator,
                          "scope": c.strategy_id or ("GLOBAL" if c.command.value.endswith("KILL_SWITCH") else ""),
                          "target": c.target or "", "status": c.status.value, "result": c.result_message or "",
                          "processed": c.processed_at} for c in repos.commands.recent(limit)])


def model_status(repos: Repositories) -> pd.DataFrame:
    return pd.DataFrame([{"run": r.run_id, "type": r.model_type.value, "version": r.model_version,
                          "strategy": r.strategy_id or "", "status": r.status.value,
                          "trained": f"{r.training_start:%Y-%m-%d} to {r.training_end:%Y-%m-%d}"}
                         for r in repos.model_runs.list()])


def pending_approvals(repos: Repositories) -> pd.DataFrame:
    waiting = (ModelRunStatus.PAPER_READY, ModelRunStatus.LIVE_CANDIDATE)
    return pd.DataFrame([{"run": r.run_id, "type": r.model_type.value, "version": r.model_version,
                          "strategy": r.strategy_id or "", "status": r.status.value}
                         for r in repos.model_runs.list() if r.status in waiting])


def learning_candidates(repos: Repositories, strategies: Sequence[str]) -> pd.DataFrame:
    rows = []
    for sid in strategies:
        for e in repos.learning_events.list_for_strategy(sid):
            rows.append({"time": e.created_at, "strategy": sid, "stage": e.stage.value,
                         "candidate": e.candidate_version or "", "outcome": e.outcome or "",
                         "approved_by": e.approved_by or ""})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def inr(v: float | None, decimals: int = 0) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}₹{abs(v):,.{decimals}f}"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def ratio(v: float | None) -> str:
    if v is None:
        return "—"
    return "∞" if v == float("inf") else f"{v:.2f}"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


NowFn = Callable[[], datetime]
