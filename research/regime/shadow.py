"""Regime and Momentum Pulse at each shadow signal (decision-engine D4).

For every shadow outcome row: the Momentum Pulse at the signal time, with
cutoffs fitted ONLY on history checkpoints of sessions before the signal's day
(point in time), and the session regime (``research.regime.regime``). The row
also says which hypotheses the latest validation KEPT, so a reader can tell a
label that has passed the out-of-sample test from one that has not.

Labels are descriptions for later shadow evaluation (D7); nothing here can
affect a trade.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from research.regime.pulse import Cutoffs, pulse
from research.regime.samples import Bar, DailyHistory, SampleSettings, features_at, regime_at, session_series


@dataclass(frozen=True)
class RegimeContext:
    settings: SampleSettings
    regime_version: str
    daily: DailyHistory
    expiries: set[date]
    bars_for: Callable[[date], Mapping[time, Bar]]  # the day's NIFTY 1-minute bars by START time
    vix_at: Callable[[datetime], float | None]  # India VIX at or just before a time
    cutoffs_for: Callable[[date], Cutoffs | None]  # fitted on checkpoints of sessions before the day
    kept: Sequence[str] = ()  # hypotheses the latest validation kept


def regime_row(o: Mapping[str, Any], ctx: RegimeContext) -> dict[str, Any] | None:
    t = datetime.fromisoformat(str(o["generated_at"])) if o.get("generated_at") else None
    if t is None:
        return None
    day = t.date()
    bars = ctx.bars_for(day)
    out: dict[str, Any] = {"signal_id": o["signal_id"], "sim_version": o["sim_version"],
                           "regime_version": ctx.regime_version, "kept_json": json.dumps(list(ctx.kept))}
    reg = regime_at(day, t, ctx.vix_at(t), ctx.daily, ctx.expiries, ctx.settings)
    out.update({"regime_named": reg["named"], "regime_trend": reg["trend"], "regime_vol": reg["vol"],
                "vol_change": reg["vol_change"], "expiry_day": int(bool(reg["expiry_day"])),
                "regime_confidence": reg["confidence"], "regime_reasons_json": json.dumps(reg["reasons"])})
    starts = sorted(bars)
    ends = [(datetime.combine(day, st) + timedelta(minutes=1)).time() for st in starts]
    usable = [k for k, e in enumerate(ends) if e <= t.time()]
    cut = ctx.cutoffs_for(day)
    if not usable or cut is None:
        out.update({"pulse_label": "UNCERTAIN", "pulse_reasons_json": json.dumps(
            ["no bars for the day" if not usable else "no Pulse cutoffs (too little earlier history)"])})
        return out
    ser = session_series({st: bars[st] for st in starts[:usable[-1] + 1]}, ctx.settings)
    f = features_at(ser, usable[-1])
    p = pulse(f, cut)
    signal_dir = 1 if o.get("option_type") == "CE" else -1
    out.update({"pulse_label": p.label, "pulse_direction": p.direction, "pulse_confidence": p.confidence,
                "pulse_aligned": int(p.direction == signal_dir) if p.direction else None,
                "pulse_reasons_json": json.dumps(list(p.reasons)),
                "er": f["er"], "adx": f["adx"], "disp": f["disp"], "atrx": f["atrx"],
                "ml_trend": f["ml_trend"], "ml_vol": f["ml_vol"]})
    return out


def regime_rows(outcomes: Sequence[Mapping[str, Any]], ctx: RegimeContext) -> list[dict[str, Any]]:
    return [r for r in (regime_row(o, ctx) for o in outcomes) if r is not None]


__all__ = ["RegimeContext", "regime_row", "regime_rows"]
