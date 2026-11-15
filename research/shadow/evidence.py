"""Volatility evidence for each shadow outcome (decision-engine D3).

For every simulated signal (``research.shadow.run`` outcome rows), at the
decision time:

* **IV at entry**: the feed IV recorded with the quote (decimal) and an IV
  solved from the quote's mid (Black-Scholes, ``volatility.options``); the
  feed value is used when its unit checks out against the solved one.
* **Greeks** of the contract and of the 1-lot position (rupees).
* **Hold economics** over ``horizon_minutes`` (capped at the square-off):
  decay in BUSINESS time (``volatility.position.hold_decay``) and, for
  contrast, calendar theta; round-trip costs per unit (charges, the spread
  crossed, slippage); the favourable NIFTY move needed to break even; and the
  expected 1-sd move over the same window under each clock convention.
  ``breakeven_to_move`` > 1 means the trade needs more than a typical move just
  to cover time and costs.

And for FILLED outcomes, over the simulated hold (ex post):

* **IV vs realised on the matched window**: realised variance of NIFTY 1-minute
  closes over the hold against the business-time implied variance of the same
  minutes (a ratio of variances; no annualisation involved).
* **IV change** entry -> exit and a **P&L attribution** of the option's mid
  change into delta, gamma, vega and decay (plus the unexplained residual).

Nothing here changes an outcome: evidence rows are stored next to them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from volatility.business_time import (
    CONVENTIONS,
    ExpiryClock,
    business_minute_variance,
    clock_from_sessions,
    expected_move,
    realised_window_variance,
    window_variance,
)
from volatility.options import calendar_years, greeks, implied_vol, infer_iv_unit
from volatility.position import attribute, breakeven_move, hold_decay

SESSION_CLOSE = time(15, 30)


@dataclass(frozen=True)
class VolContext:
    r: float
    q: float
    horizon_minutes: int
    square_off: time
    tick_size: float
    slippage_ticks: int
    evidence_version: str
    overnight_share: Callable[[date], float]  # point in time: from sessions before the day
    gap_weights: Callable[[date, date], list[float]]  # (day, expiry) -> one weight per later session to expiry
    option_quote: Callable[[str, datetime], Mapping[str, Any] | None]  # latest archived quote at or before
    index_closes: Callable[[datetime, datetime], list[float]]  # NIFTY 1-minute closes, start..end inclusive
    cost_fn: Callable[[int, float, float], float]


def gap_weights(day: date, expiry: date, is_trading_day: Callable[[date], bool], weekend_weight: float) -> list[float]:
    """One weight per trading session after ``day`` up to ``expiry``: 1 for a
    normal night, ``weekend_weight`` when the gap spans a weekend or holiday."""
    out: list[float] = []
    prev, d = day, day + timedelta(days=1)
    while d <= expiry:
        if is_trading_day(d):
            out.append(1.0 if (d - prev).days == 1 else weekend_weight)
            prev = d
        d += timedelta(days=1)
    return out


def weekend_gap_weight(weekend_variance_multiple: float, overnight_share: float) -> float:
    """Gap weight of a weekend from R2's close-to-close multiple ``m``: the extra
    variance sits in the gap, so ``(m - (1 - w)) / w`` normal gaps (at least 1)."""
    if overnight_share <= 0:
        return 1.0
    return max(1.0, (weekend_variance_multiple - (1.0 - overnight_share)) / overnight_share)


def _mid(q: Mapping[str, Any]) -> float | None:
    bid, ask, ltp = q.get("bid"), q.get("ask"), q.get("ltp")
    if bid and ask and ask >= bid:
        return (float(bid) + float(ask)) / 2
    return float(ltp) if ltp else None


def _dt(v: Any) -> datetime | None:
    return None if v is None else datetime.fromisoformat(str(v))


def _clock(at: datetime, expiry: date, ctx: VolContext, weights: list[float]) -> ExpiryClock | None:
    close_dt = datetime.combine(expiry, SESSION_CLOSE, tzinfo=at.tzinfo)
    minutes_cal = (close_dt - at).total_seconds() / 60
    if minutes_cal <= 0:
        return None
    today_close = datetime.combine(at.date(), SESSION_CLOSE, tzinfo=at.tzinfo)
    left_today = max(0.0, (today_close - at).total_seconds() / 60)
    return clock_from_sessions(calendar_years(minutes_cal), left_today, weights)


def _iv(q: Mapping[str, Any], spot: float, strike: float, years: float, ctx: VolContext,
        call: bool) -> tuple[float | None, float | None, float | None, str | None]:
    """(iv used, feed iv, solved iv, feed unit)."""
    mid = _mid(q)
    solved = implied_vol(mid, spot, strike, years, ctx.r, ctx.q, call) if mid else None
    feed = q.get("iv")
    feed = float(feed) if feed else None
    unit = infer_iv_unit(feed, solved) if feed and solved else None
    used = feed if unit == "decimal" else (feed / 100.0 if unit == "percent" and feed else solved)
    return used, feed, solved, unit


def evidence_row(o: Mapping[str, Any], ctx: VolContext) -> dict[str, Any] | None:
    """Evidence for one outcome row; None when there is no contract, spot or decision quote."""
    t, key, strike, expiry_s = _dt(o.get("generated_at")), o.get("instrument_key"), o.get("strike"), o.get("expiry")
    spot = o.get("spot")
    if t is None or not key or strike is None or not expiry_s or not spot:
        return None
    expiry = date.fromisoformat(str(expiry_s))
    call = o["option_type"] == "CE"
    q0 = ctx.option_quote(str(key), t)
    if q0 is None:
        return None
    w = ctx.overnight_share(t.date())
    weights = ctx.gap_weights(t.date(), expiry)
    clock = _clock(t, expiry, ctx, weights)
    if clock is None:
        return None
    iv0, feed0, solved0, unit0 = _iv(q0, float(spot), float(strike), clock.calendar_years, ctx, call)
    out: dict[str, Any] = {
        "signal_id": o["signal_id"], "sim_version": o["sim_version"], "evidence_version": ctx.evidence_version,
        "overnight_share": round(w, 4), "minutes_to_expiry": round(clock.calendar_years * 525_600, 1),
        "trading_minutes_to_expiry": clock.trading_minutes, "gap_weight_to_expiry": round(clock.gap_weight, 3),
        "iv_feed": feed0, "iv_solved": None if solved0 is None else round(solved0, 5), "iv_feed_unit": unit0,
        "iv_entry": None if iv0 is None else round(iv0, 5),
    }
    if iv0 is None or iv0 <= 0:
        return out
    S, K, years = float(spot), float(strike), clock.calendar_years
    g = greeks(S, K, years, iv0, ctx.r, ctx.q, call)
    if g is None:
        return out
    lot = int(o.get("lot_size") or 1)
    to_square_off = (datetime.combine(t.date(), ctx.square_off, tzinfo=t.tzinfo) - t).total_seconds() / 60
    h = max(1.0, min(float(ctx.horizon_minutes), to_square_off))
    var = {c: window_variance(iv0, h, c, clock=clock, overnight_share=w) for c in CONVENTIONS}
    em = {c: expected_move(S, v) for c, v in var.items()}
    decay = hold_decay(S, K, iv0, clock, h, ctx.r, ctx.q, call, overnight_share=w)
    bid, ask = q0.get("bid"), q0.get("ask")
    spread = float(ask) - float(bid) if bid and ask and ask >= bid else 0.0
    entry = float(ask) if ask else (_mid(q0) or 0.0)
    charges = ctx.cost_fn(lot, entry, entry) / lot if entry > 0 else 0.0
    costs = spread + 2 * ctx.slippage_ticks * ctx.tick_size + charges
    be = breakeven_move(g.delta, g.gamma, decay + costs)
    out.update({
        "delta": round(g.delta, 5), "gamma": round(g.gamma, 7), "vega": round(g.vega, 4),
        "theta_day": round(g.theta_day, 4),
        "position_delta_rupees": round(lot * g.delta, 2), "position_gamma_rupees": round(lot * g.gamma, 4),
        "position_vega_rupees": round(lot * g.vega, 2), "position_theta_rupees_day": round(lot * g.theta_day, 2),
        "horizon_minutes": h, "decay_business": round(decay, 4), "decay_calendar": round(-g.theta_minute * h, 4),
        "costs_per_unit": round(costs, 4), "breakeven_points": None if math.isinf(be) else round(be, 2),
        "expected_move_calendar": round(em["calendar"], 2), "expected_move_trading": round(em["trading"], 2),
        "expected_move_business": round(em["business"], 2),
        "breakeven_to_move": None if math.isinf(be) or em["business"] <= 0 else round(be / em["business"], 3),
    })
    if o.get("status") != "FILLED":
        return out
    t_in, t_out = _dt(o.get("entry_time")), _dt(o.get("exit_time"))
    if t_in is None or t_out is None or t_out <= t_in or t_out.date() != t_in.date():
        return out
    hold = (t_out - t_in).total_seconds() / 60
    closes = ctx.index_closes(t_in - timedelta(minutes=1), t_out)
    rv = realised_window_variance(closes)
    iv_var = business_minute_variance(iv0, clock, w) * hold
    out.update({"hold_minutes": round(hold, 1), "realised_var_hold": None if math.isnan(rv) else rv,
                "implied_var_hold": iv_var,
                "realised_to_implied": None if math.isnan(rv) or iv_var <= 0 else round(rv / iv_var, 3)})
    q1 = ctx.option_quote(str(key), t_out)
    if q1 is None or len(closes) < 2:
        return out
    s1 = closes[-1]
    clock1 = _clock(t_out, expiry, ctx, weights)
    if clock1 is None:
        return out
    iv1, *_ = _iv(q1, s1, K, clock1.calendar_years, ctx, call)
    m0, m1 = _mid(q0), _mid(q1)
    if iv1 is None or m0 is None or m1 is None:
        return out
    att = attribute(g, d_spot=s1 - closes[0], d_iv_points=(iv1 - iv0) * 100,
                    decay=hold_decay(S, K, iv0, clock, hold, ctx.r, ctx.q, call, overnight_share=w), actual=m1 - m0)
    out.update({"iv_exit": round(iv1, 5), "iv_change_points": round((iv1 - iv0) * 100, 3),
                "pnl_delta": round(att.delta, 3), "pnl_gamma": round(att.gamma, 3), "pnl_vega": round(att.vega, 3),
                "pnl_decay": round(att.theta, 3), "pnl_explained": round(att.explained, 3),
                "pnl_actual_mid": round(att.actual, 3), "pnl_residual": round(att.residual, 3)})
    return out


def evidence_rows(outcomes: Sequence[Mapping[str, Any]], ctx: VolContext) -> list[dict[str, Any]]:
    return [e for e in (evidence_row(o, ctx) for o in outcomes) if e is not None]


__all__ = ["SESSION_CLOSE", "VolContext", "evidence_row", "evidence_rows", "gap_weights", "weekend_gap_weight"]
