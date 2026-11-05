"""Evaluation metrics for ML models (all computed on out-of-sample rows).

Statistical metrics
    binary:      ROC AUC, Brier score, log loss, base rate
    multiclass:  log loss and accuracy vs. the training-prior baseline
    regression:  MAE, RMSE, R^2 vs. the training-mean baseline

Trading metrics (``trading_metrics``), required by the ml-rl-trading-safety
Skill, on the triple-barrier outcome of each event:

* every event is sized like ``risk.position_sizer``: lots =
  floor(risk_per_trade * size_multiplier / (stop distance * lot units)); an
  event that sizes to 0 lots is not traded (the risk gate would refuse it)
* net P&L per trade = net P&L per lot (charges + slippage included) * lots
* reported: trades, win rate, profit factor, cost-adjusted expectancy, net
  P&L, charges, max drawdown (closed-trade equity), average holding minutes
* risk compliance: multiplier always in [0, 1], every trade has a stop below
  entry, no entry at/after the no-new-entries cut-off, no exit after the
  square-off start, loss per trade within the per-trade risk budget (WARN on
  gaps, like the backtester)

This is an EVENT-level evaluation: it does not model overlapping positions,
daily caps, cooldowns or the kill switch. The full event-driven backtest
(``backtest/``) is the Phase 9 VERIFY step for any candidate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
EPS = 1e-12


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


def roc_auc(y: npt.ArrayLike, score: npt.ArrayLike) -> float | None:
    y_arr, s = np.asarray(y).astype(int), np.asarray(score, dtype=np.float64)
    pos, neg = int(y_arr.sum()), int((1 - y_arr).sum())
    if pos == 0 or neg == 0:
        return None
    ranks = pd.Series(s).rank(method="average").to_numpy()
    return float((ranks[y_arr == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def binary_metrics(y: npt.ArrayLike, p: npt.ArrayLike) -> dict[str, float | None]:
    y_arr, p_arr = np.asarray(y).astype(np.float64), np.clip(np.asarray(p, dtype=np.float64), EPS, 1 - EPS)
    if y_arr.size == 0:
        return {"n": 0, "auc": None, "brier": None, "log_loss": None, "base_rate": None, "mean_score": None}
    return {"n": int(y_arr.size), "auc": _r(roc_auc(y_arr, p_arr)),
            "brier": _r(float(np.mean((p_arr - y_arr) ** 2))),
            "log_loss": _r(float(-np.mean(y_arr * np.log(p_arr) + (1 - y_arr) * np.log(1 - p_arr)))),
            "base_rate": _r(float(y_arr.mean())), "mean_score": _r(float(p_arr.mean()))}


def multiclass_metrics(y: npt.ArrayLike, proba: npt.ArrayLike, prior: Sequence[float]) -> dict[str, float | None]:
    y_arr = np.asarray(y).astype(int)
    P = np.clip(np.asarray(proba, dtype=np.float64), EPS, 1.0)
    if y_arr.size == 0:
        return {"n": 0, "log_loss": None, "accuracy": None, "baseline_log_loss": None, "baseline_accuracy": None}
    pri = np.clip(np.asarray(prior, dtype=np.float64), EPS, 1.0)
    return {"n": int(y_arr.size), "log_loss": _r(float(-np.mean(np.log(P[np.arange(y_arr.size), y_arr])))),
            "accuracy": _r(float(np.mean(P.argmax(axis=1) == y_arr))),
            "baseline_log_loss": _r(float(-np.mean(np.log(pri[y_arr])))),
            "baseline_accuracy": _r(float(np.mean(y_arr == int(np.argmax(pri)))))}


def regression_metrics(y: npt.ArrayLike, pred: npt.ArrayLike, train_mean: float) -> dict[str, float | None]:
    y_arr, p = np.asarray(y, dtype=np.float64), np.asarray(pred, dtype=np.float64)
    if y_arr.size == 0:
        return {"n": 0, "mae": None, "rmse": None, "r2": None, "baseline_mae": None}
    ss_res = float(np.sum((y_arr - p) ** 2))
    ss_tot = float(np.sum((y_arr - train_mean) ** 2))
    return {"n": int(y_arr.size), "mae": _r(float(np.mean(np.abs(y_arr - p)))),
            "rmse": _r(math.sqrt(ss_res / y_arr.size)), "r2": _r(1 - ss_res / ss_tot) if ss_tot > 0 else None,
            "baseline_mae": _r(float(np.mean(np.abs(y_arr - train_mean))))}


def seed_spread(values: Sequence[float | None]) -> dict[str, float | None]:
    v = [x for x in values if x is not None and math.isfinite(x)]
    if not v:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    return {"mean": _r(float(np.mean(v))), "std": _r(float(np.std(v, ddof=1)) if len(v) > 1 else 0.0),
            "min": _r(min(v)), "max": _r(max(v)), "n": len(v)}


# --------------------------------------------------------------------------- #
# Trading metrics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradingMetrics:
    events: int
    trades: int
    skipped_zero_lots: int
    wins: int
    win_rate_pct: float | None
    profit_factor: float | None
    expectancy: float | None  # net rupees per trade, after charges and slippage
    net_pnl: float
    gross_pnl: float
    charges: float
    max_drawdown: float
    avg_holding_minutes: float | None
    avg_size_multiplier: float | None
    compliance: dict[str, str] = field(default_factory=dict)  # check -> OK | WARN | BREACH

    @property
    def compliance_status(self) -> str:
        vals = set(self.compliance.values())
        return "FAIL" if "BREACH" in vals else ("PASS WITH WARNINGS" if "WARN" in vals else "PASS")

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "compliance_status": self.compliance_status}


def trading_metrics(labels: pd.DataFrame, multipliers: npt.ArrayLike, *, risk_per_trade: float,
                    no_new_entries_after: time, square_off_start: time) -> TradingMetrics:
    """``labels``: TrainingSet label rows (one per event, chronological);
    ``multipliers``: the filter's size multiplier per event (0 = not taken)."""
    mult = np.asarray(multipliers, dtype=np.float64)
    if mult.size != len(labels):
        raise ValueError("one multiplier per event")
    risk_unit = (labels["entry"] - labels["stop"]).to_numpy(dtype=np.float64)
    lot = labels["lot_units"].to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        lots = np.floor(np.where(risk_unit * lot > 0, risk_per_trade * mult / (risk_unit * lot), 0.0))
    take = mult > 0
    traded = take & (lots >= 1)
    pnl = labels["net_pnl_per_lot"].to_numpy(dtype=np.float64) * lots
    fees = labels["charges_per_lot"].to_numpy(dtype=np.float64) * lots
    tp, tf = pnl[traded], fees[traded]
    wins = tp[tp > 0]
    gross_loss = -float(tp[tp < 0].sum())
    equity = np.cumsum(tp)
    peak = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:] if tp.size else np.zeros(0)
    dd = float(np.max(peak - equity)) if tp.size else 0.0
    t0 = labels["t0"][traded]
    t1 = labels["t1"][traded]
    breaches = {
        "size multiplier within [0, 1]": "OK" if np.all((mult >= 0) & (mult <= 1)) else "BREACH",
        "every trade has a stop below entry": "OK" if np.all(risk_unit[traded] > 0) else "BREACH",
        f"no entries at/after {no_new_entries_after:%H:%M}":
            "OK" if all(t.time() < no_new_entries_after for t in t0) else "BREACH",
        f"closed by the {square_off_start:%H:%M} square-off start":
            "OK" if all(t.time() <= square_off_start for t in t1) else "BREACH",
        "loss per trade within the per-trade risk budget (gaps can overshoot)":
            "OK" if np.all(tp >= -risk_per_trade) else "WARN",
    }
    hold = labels["holding_minutes"].to_numpy(dtype=np.float64)[traded]
    return TradingMetrics(
        events=len(labels), trades=int(traded.sum()), skipped_zero_lots=int((take & ~traded).sum()),
        wins=int(wins.size), win_rate_pct=_r(wins.size / tp.size * 100, 2) if tp.size else None,
        profit_factor=_r(float(wins.sum()) / gross_loss, 3) if gross_loss > 0 else None,
        expectancy=_r(float(tp.mean()), 2) if tp.size else None, net_pnl=round(float(tp.sum()), 2),
        gross_pnl=round(float(tp.sum() + tf.sum()), 2), charges=round(float(tf.sum()), 2), max_drawdown=round(dd, 2),
        avg_holding_minutes=_r(float(hold.mean()), 2) if hold.size else None,
        avg_size_multiplier=_r(float(mult[take].mean()), 4) if take.any() else None, compliance=breaches)


__all__ = ["TradingMetrics", "binary_metrics", "multiclass_metrics", "regression_metrics", "roc_auc", "seed_spread",
           "trading_metrics"]
