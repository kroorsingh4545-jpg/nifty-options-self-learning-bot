"""Intraday IV vs realised on matched horizons (decision-engine D3, history).

Question: when a trade is held for 15-60 minutes, how should an annualised
implied volatility (India VIX, calendar time) be turned into the variance to
expect over the hold? ``volatility.business_time`` offers three clocks
(calendar, trading, business); this study measures which one matches what
NIFTY actually did, on every session of the stored 1-minute history.

Point in time: at each checkpoint (e.g. 10:00) the forecast uses the VIX close
of the minute ending at the checkpoint and an overnight share measured on
sessions BEFORE the day; the outcome is the realised variance (sum of squared
1-minute log returns) over the next ``h`` minutes of the same session. A
window with a missing minute is skipped.

Metrics per (clock, horizon): variance ratio realised / implied (1 = right on
average; block-bootstrap 95 % interval over time-ordered windows), the share
of moves inside +/- 1 and 2 implied sd against the normal 68.3 % / 95.4 %, and
QLIKE (lower is better), plus the ratio by checkpoint and by year for the
business clock.

Caveats: VIX is a 30-day, all-strike measure, not the IV of the ATM weekly a
strategy buys; the VIX clock assumes 252/365 sessions per calendar day with
equal gaps; 1-minute index closes carry a little staleness noise.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from volatility.business_time import CONVENTIONS, overnight_share, vix_clock, window_variance
from volatility.calibration import band_hit_rate, block_bootstrap_ratio_ci, qlike, variance_ratio

#: business-clock ratio by checkpoint / year: groups need at least this many windows
MIN_GROUP = 30


def minute_closes_by_day(df: pd.DataFrame) -> dict[date, dict[time, float]]:
    """{session day: {candle END time: close}} from minute candles (start_time, close)."""
    out: dict[date, dict[time, float]] = {}
    for start, close in zip(df["start_time"], df["close"], strict=True):
        st = datetime.fromisoformat(str(start))
        end = st + timedelta(minutes=1)
        out.setdefault(st.date(), {})[end.time()] = float(close)
    return out


def trailing_overnight_shares(daily: pd.DataFrame, *, lookback: int, reliable_from: date,
                              default: float) -> dict[date, float]:
    """{day: overnight share over the ``lookback`` sessions before it}; ``daily``
    has session_date, o, c (sorted). Days without enough reliable history get ``default``."""
    d = daily[daily["session_date"] >= reliable_from].reset_index(drop=True)
    days = list(d["session_date"])
    prev = d["c"].shift(1).tolist()
    o, c = d["o"].tolist(), d["c"].tolist()
    out: dict[date, float] = {}
    for i, day in enumerate(days):
        lo = max(1, i - lookback)
        if i - lo < max(20, lookback // 4):
            out[day] = default
            continue
        w = overnight_share(prev[lo:i], o[lo:i], c[lo:i])
        out[day] = default if math.isnan(w) else w
    return out


def _add(t: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def windows(nifty: Mapping[date, Mapping[time, float]], vix: Mapping[date, Mapping[time, float]],
            shares: Mapping[date, float], *, checkpoints: Sequence[time], horizons: Sequence[int],
            default_share: float) -> pd.DataFrame:
    """One row per (day, checkpoint, horizon) with complete data: realised
    variance and the implied variance under each clock."""
    rows = []
    clock = vix_clock()
    for day in sorted(nifty):
        n, v = nifty[day], vix.get(day, {})
        w = shares.get(day, default_share)
        for cp in checkpoints:
            if cp not in n or cp not in v or not v[cp] > 0:
                continue
            iv = v[cp] / 100.0  # India VIX is quoted in percent
            for h in horizons:
                path = [n.get(_add(cp, k)) for k in range(h + 1)]
                if any(p is None for p in path):
                    continue
                closes = np.asarray(path, dtype=np.float64)
                r = np.diff(np.log(closes))
                row: dict[str, Any] = {"day": day, "checkpoint": cp.strftime("%H:%M"), "horizon": h,
                                       "realised": float(np.sum(r * r)),
                                       "move": float(math.log(closes[-1] / closes[0])), "vix": v[cp],
                                       "overnight_share": w}
                for conv in CONVENTIONS:
                    row[conv] = window_variance(iv, h, conv, clock=clock, overnight_share=w)
                rows.append(row)
    return pd.DataFrame(rows)


def _metrics(g: pd.DataFrame, conv: str, block: int) -> dict[str, Any]:
    rv, iv = g["realised"].to_numpy(), g[conv].to_numpy()
    z = g["move"].to_numpy() / np.sqrt(iv)
    lo, hi = block_bootstrap_ratio_ci(rv, iv, block=block)
    return {"n": len(g), "variance_ratio": round(variance_ratio(rv, iv), 3),
            "ci95": [None if math.isnan(lo) else round(lo, 3), None if math.isnan(hi) else round(hi, 3)],
            "hit_1sd": band_hit_rate(z, 1.0).as_dict(), "hit_2sd": band_hit_rate(z, 2.0).as_dict(),
            "qlike": round(qlike(rv, iv), 4)}


def study(df: pd.DataFrame, *, block: int = 20) -> dict[str, Any]:
    """The report (JSON-ready) from ``windows``."""
    if df.empty:
        return {"windows": 0}
    df = df.sort_values(["day", "checkpoint"]).reset_index(drop=True)
    out: dict[str, Any] = {"windows": len(df), "sessions": int(df["day"].nunique()),
                           "from": str(df["day"].min()), "to": str(df["day"].max()),
                           "median_overnight_share": round(float(df["overnight_share"].median()), 3), "by_horizon": {}}
    for h, g in df.groupby("horizon"):
        out["by_horizon"][str(h)] = {conv: _metrics(g, conv, block) for conv in CONVENTIONS}
    biz = df[df["horizon"] == df["horizon"].median()] if df["horizon"].nunique() > 1 else df
    out["business_by_checkpoint"] = {cp: round(variance_ratio(g["realised"], g["business"]), 3)
                                     for cp, g in biz.groupby("checkpoint") if len(g) >= MIN_GROUP}
    years = pd.Series([d.year for d in biz["day"]], index=biz.index)
    out["business_by_year"] = {str(y): round(variance_ratio(g["realised"], g["business"]), 3)
                               for y, g in biz.groupby(years) if len(g) >= MIN_GROUP}
    out["by_checkpoint_horizon"] = int(biz["horizon"].iloc[0])
    return out


def render_text(rep: Mapping[str, Any]) -> list[str]:
    if not rep.get("windows"):
        return ["no complete intraday windows in the stored history"]
    lines = [f"Intraday IV vs realised: {rep['windows']:,} windows on {rep['sessions']:,} sessions "
             f"({rep['from']} .. {rep['to']}); median overnight share {rep['median_overnight_share']}",
             "variance ratio = realised / implied (1 = right on average); inside 1 sd: normal 68.3 %",
             "horizon  clock      windows  ratio (95% CI)        inside 1sd  inside 2sd  QLIKE"]
    for h, convs in rep["by_horizon"].items():
        for conv, m in convs.items():
            ci = m["ci95"]
            lines.append(f"{h:>4} min  {conv:9s} {m['n']:8,}  {m['variance_ratio']:6.3f} ({ci[0]}-{ci[1]})"
                         f"{'':4s} {100 * m['hit_1sd']['rate']:8.1f} %  {100 * m['hit_2sd']['rate']:8.1f} %  "
                         f"{m['qlike']:.3f}")
    lines.append(f"business clock, {rep['by_checkpoint_horizon']}-minute windows, ratio by checkpoint: "
                 + ", ".join(f"{k} {v}" for k, v in rep["business_by_checkpoint"].items()))
    lines.append("... by year: " + ", ".join(f"{k} {v}" for k, v in rep["business_by_year"].items()))
    return lines


__all__ = ["MIN_GROUP", "minute_closes_by_day", "render_text", "study", "trailing_overnight_shares", "windows"]
