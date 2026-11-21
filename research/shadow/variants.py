"""Forward shadow variants A and B on recorded quotes (pure; rules sv.v1).

``research/shadow/variant_rules.py`` holds the pre-registered rules. Both
variants are long-option trades on the bot's recorded quotes with an exit
driven by the NIFTY 1-minute closes instead of the premium:

* **A** (``A_ORB_SOURCE_EXIT``): every orb_options signal of the D2 shadow,
  same contract and entry as its D2 row; stop = a 1-minute close beyond the
  opposite side of the 09:15-09:29 range; no target, no trailing; square-off.
* **B** (``B_RETEST30_LONG_ATM``): the E1 30-minute retest signal (filter
  B_RETEST_30) on the session's bars, long ATM; stop = a later close beyond the
  opposite side of the 30-minute range; square-off.

The entry is the D2 simulator's entry (``research/shadow/simulate.py``),
restated here so the D2 simulator stays untouched;
``tests/unit/test_shadow_variants.py`` checks that both give the same entry on
the same quotes. Nothing here can place an order or change trading.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from research.orb_study.data import SQUARE_OFF_BAR, bar_close, make_session
from research.orb_study.registration import STUDY_E1
from research.orb_study.setups import RETEST, filtered, source_entries
from research.orb_study.stats import bootstrap_means
from research.shadow.rules import END_OF_DATA, SQUARE_OFF, STOP_LOSS, TradeRules
from research.shadow.simulate import (
    FILLED,
    NO_CONTRACT,
    NO_FILL,
    NO_QUOTE,
    NO_TWO_SIDED,
    Contract,
    Quote,
    liquidity_reasons,
    select_contract,
)
from research.shadow.variant_rules import EVALUATION_FROM, VARIANTS_V1, VARIANTS_VERSION

A_NAME, B_NAME = str(VARIANTS_V1["A"]["name"]), str(VARIANTS_V1["B"]["name"])
NO_RANGE, NO_BASE = "NO_RANGE", "NO_BASE"
ORB_RANGE = (time(9, 15), time(9, 30))  # bars starting 09:15 .. 09:29
SESSION_CLOSE = time(15, 30)
Bar = tuple[float, float, float, float]

@dataclass
class Trade:
    status: str
    flags: list[str] = field(default_factory=list)
    liquidity: list[str] = field(default_factory=list)
    decision_quote_at: datetime | None = None
    entry_time: datetime | None = None
    entry_price: float | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    trigger_at: datetime | None = None
    quantity: int = 0
    gross: float | None = None
    charges: float | None = None
    net: float | None = None


def _ceil(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 2)


def _sell(ref: float, r: TradeRules) -> float:
    steps = math.floor((ref - r.slippage_ticks * r.tick_size) / r.tick_size + 1e-9)
    return round(max(r.tick_size, steps * r.tick_size), 2)


def enter(decision_at: datetime, quotes: Sequence[Quote], r: TradeRules, qty: int) -> tuple[Trade, int | None]:
    """The D2 simulator's decision quote and entry fill (``quotes`` sorted by receipt)."""
    before = [i for i, q in enumerate(quotes) if q.received <= decision_at]
    late = False
    if before and (decision_at - quotes[before[-1]].received).total_seconds() <= r.max_quote_age_seconds:
        i0 = before[-1]
    else:
        limit_t = decision_at + timedelta(seconds=r.entry_timeout_seconds)
        nxt = next((i for i, q in enumerate(quotes) if decision_at < q.received <= limit_t), None)
        if nxt is None:
            return Trade(NO_QUOTE), None
        i0, late = nxt, True
    dq = quotes[i0]
    t = Trade(NO_FILL, decision_quote_at=dq.received, liquidity=liquidity_reasons(dq, r))
    if late:
        t.flags.append("late_decision_quote")
    if not dq.bid or not dq.ask:
        t.status = NO_TWO_SIDED
        return t, None
    limit = _ceil(dq.ask + r.entry_buffer_ticks * r.tick_size, r.tick_size)
    deadline = dq.received + timedelta(seconds=r.entry_timeout_seconds)
    for i in range(i0, len(quotes)):
        q = quotes[i]
        if q.received > deadline:
            break
        ref = q.ask if q.ask else q.ltp
        if ref > limit or (q.ask_qty and q.ask_qty < qty):
            continue
        price = round(max(r.tick_size, math.ceil((ref + r.slippage_ticks * r.tick_size) / r.tick_size - 1e-9)
                          * r.tick_size), 2)
        t.status, t.entry_price, t.entry_time, t.quantity = FILLED, min(price, limit), q.received, qty
        return t, i
    return t, None


def stop_trigger(bars: Mapping[time, Bar], day: date, tz: Any, *, after: datetime, hi: float, lo: float,
                 call: bool, square_off: time, delay: float) -> datetime | None:
    """When the first 1-minute close beyond the opposite range side is acted on:
    bars ending at or after ``after`` and before ``square_off``; None = no stop."""
    for start in sorted(bars):
        end = datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=1)
        if end < after:
            continue
        if end.time() >= square_off:
            break
        c = bars[start][3]
        if (c < lo) if call else (c > hi):
            return end + timedelta(seconds=delay)
    return None


def run_trade(decision_at: datetime, quotes: Sequence[Quote], r: TradeRules, *, lot_size: int,
              cost_fn: Callable[[int, float, float], float],
              stop_at: Callable[[datetime], datetime | None]) -> Trade:
    """Enter as D2, then exit at the stop trigger (``stop_at(entry fill time)``) or the square-off."""
    qs = sorted(quotes, key=lambda q: q.received)
    t, ei = enter(decision_at, qs, r, lot_size)
    if ei is None or t.entry_time is None or t.entry_price is None:
        return t
    square = datetime.combine(decision_at.date(), r.square_off_start, tzinfo=decision_at.tzinfo)
    stop = stop_at(t.entry_time)
    trigger, reason = (stop, STOP_LOSS) if stop is not None and stop < square else (square, SQUARE_OFF)
    t.trigger_at = trigger
    exit_q: Quote | None = None
    for q in qs[ei + 1:]:
        if q.received < trigger:
            continue
        if q.bid_qty and q.bid_qty < t.quantity:
            if "exit_waited_for_depth" not in t.flags:
                t.flags.append("exit_waited_for_depth")
            continue
        exit_q = q
        break
    if exit_q is None:
        exit_q = qs[-1] if len(qs) > ei + 1 else qs[ei]
        reason = END_OF_DATA
        t.flags.append("ended_before_exit_trigger")
    ref = exit_q.bid if exit_q.bid else exit_q.ltp
    if reason == STOP_LOSS:
        ref = min(ref, exit_q.ltp)
    t.exit_price, t.exit_time, t.exit_reason = _sell(ref, r), exit_q.received, reason
    t.gross = round((t.exit_price - t.entry_price) * t.quantity, 2)
    t.charges = round(cost_fn(t.quantity, t.entry_price, t.exit_price), 2)
    t.net = round(t.gross - t.charges, 2)
    return t


def orb_range(bars: Mapping[time, Bar]) -> tuple[float, float] | None:
    """(high, low) of the bars starting 09:15 .. 09:29; None when a minute is missing."""
    inside = [b for t, b in bars.items() if ORB_RANGE[0] <= t < ORB_RANGE[1]]
    if len(inside) < 15:
        return None
    return max(b[1] for b in inside), min(b[2] for b in inside)


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


def _row(variant: str, signal_id: str, day: date, generated_at: datetime, option_type: str, t: Trade,
         contract: Contract | None, *, base_status: str | None = None, base_net: float | None = None,
         info: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"variant": variant, "variant_version": VARIANTS_VERSION, "signal_id": signal_id, "day": day.isoformat(),
            "generated_at": generated_at.isoformat(), "option_type": option_type,
            "before_registration": int(day < EVALUATION_FROM), "status": t.status,
            "instrument_key": None if contract is None else contract.instrument_key,
            "strike": None if contract is None else contract.strike,
            "expiry": None if contract is None else contract.expiry.isoformat(),
            "lot_size": None if contract is None else contract.lot_size,
            "decision_quote_at": _iso(t.decision_quote_at), "entry_time": _iso(t.entry_time),
            "entry_price": t.entry_price, "exit_time": _iso(t.exit_time), "exit_price": t.exit_price,
            "exit_reason": t.exit_reason, "trigger_at": _iso(t.trigger_at), "quantity": t.quantity,
            "gross": t.gross, "charges": t.charges, "net": t.net, "base_status": base_status, "base_net": base_net,
            "flags_json": json.dumps(t.flags + [f"liquidity: {x}" for x in t.liquidity]),
            "info_json": json.dumps(dict(info or {}), default=str)}


@dataclass(frozen=True)
class VariantInputs:
    rules: TradeRules  # orb_options' D2 trade rules (bot configuration, read-only)
    contracts: Sequence[Contract]
    quotes: Callable[[str, datetime, datetime], list[Quote]]
    bars: Callable[[date], Mapping[time, Bar]]  # NIFTY 1-minute bars {bar START: ohlc} of a session
    cost_fn: Callable[[int, float, float], float]
    tz: Any
    delay: float = float(VARIANTS_V1["common"]["candle_delay_seconds"])


def _session_quotes(inp: VariantInputs, key: str, at: datetime) -> list[Quote]:
    start = at - timedelta(seconds=inp.rules.max_quote_age_seconds + 1)
    return inp.quotes(key, start, datetime.combine(at.date(), SESSION_CLOSE, tzinfo=at.tzinfo))


def variant_a(d2_rows: Sequence[Mapping[str, Any]], inp: VariantInputs) -> list[dict[str, Any]]:
    """A for every orb_options D2 row (same contract and entry)."""
    out = []
    for r in d2_rows:
        if r.get("strategy_id") != "orb_options":
            continue
        at = datetime.fromisoformat(str(r["generated_at"]))
        day = at.date()
        base = {"base_status": r.get("status"), "base_net": r.get("net")}
        if not r.get("instrument_key"):
            out.append(_row(A_NAME, str(r["signal_id"]), day, at, str(r["option_type"]), Trade(NO_BASE), None,
                            **base))
            continue
        c = Contract(str(r["instrument_key"]), str(r["option_type"]), float(r["strike"]),
                     date.fromisoformat(str(r["expiry"])), int(r["lot_size"]))
        bars = inp.bars(day)
        rng = orb_range(bars)
        if rng is None:
            out.append(_row(A_NAME, str(r["signal_id"]), day, at, c.option_type, Trade(NO_RANGE), c, **base))
            continue
        hi, lo = rng
        call = c.option_type == "CE"

        def stop_at(entry: datetime, bars: Mapping[time, Bar] = bars, hi: float = hi, lo: float = lo,
                    call: bool = call, day: date = day) -> datetime | None:
            return stop_trigger(bars, day, inp.tz, after=entry, hi=hi, lo=lo, call=call,
                                square_off=inp.rules.square_off_start, delay=inp.delay)

        t = run_trade(at, _session_quotes(inp, c.instrument_key, at), inp.rules, lot_size=c.lot_size,
                      cost_fn=inp.cost_fn, stop_at=stop_at)
        out.append(_row(A_NAME, str(r["signal_id"]), day, at, c.option_type, t, c, **base,
                        info={"or_high": hi, "or_low": lo}))
    return out


def retest_signal(day: date, bars: Mapping[time, Bar]) -> Any:
    """The E1 B_RETEST_30 entry of a session (or None)."""
    s = make_session(day, dict(bars))
    e = source_entries(s, 30, exit_bar=SQUARE_OFF_BAR)[RETEST]
    return e if filtered(e, STUDY_E1["filters_source"]["B_RETEST_30"]) else None


def variant_b(days: Sequence[date], inp: VariantInputs) -> list[dict[str, Any]]:
    out = []
    r = inp.rules
    for day in days:
        bars = inp.bars(day)
        e = retest_signal(day, bars) if bars else None
        if e is None:
            continue
        at = datetime.combine(day, bar_close(e.m), tzinfo=inp.tz) + timedelta(seconds=inp.delay)
        opt = "CE" if e.direction > 0 else "PE"
        sid = f"B30R-{day.isoformat()}"
        info = {"range_high": e.hi, "range_low": e.lo, "spot": e.spot, "first_break_bar": e.first_break_m,
                "entry_bar": e.m, "range_pct_of_open": round(e.range_pct_of_open, 4)}
        c = select_contract(e.spot, opt, day, inp.contracts, mode="ATM", step=r.strike_step)
        if c is None:
            out.append(_row(B_NAME, sid, day, at, opt, Trade(NO_CONTRACT), None, info=info))
            continue

        def stop_at(entry: datetime, bars: Mapping[time, Bar] = bars, e: Any = e, day: date = day) -> datetime | None:
            return stop_trigger(bars, day, inp.tz, after=entry, hi=e.hi, lo=e.lo, call=e.direction > 0,
                                square_off=r.square_off_start, delay=inp.delay)

        t = run_trade(at, _session_quotes(inp, c.instrument_key, at), r, lot_size=c.lot_size, cost_fn=inp.cost_fn,
                      stop_at=stop_at)
        out.append(_row(B_NAME, sid, day, at, opt, t, c, info=info))
    return out


# ---------------------------------------------------------------- daily report and evaluation
def _test(days: list[date], values: list[float], first_look: int, final: int) -> dict[str, Any]:
    n = len(values)
    out: dict[str, Any] = {"n": n, "first_look_at": first_look, "final_at": final,
                           "mean": None if not n else round(float(np.mean(values)), 2),
                           "total": round(float(np.sum(values)), 2) if n else 0.0}
    if n < first_look:
        out["status"] = "NOT_YET_EVALUABLE"
        out["progress"] = round(n / first_look, 4)
        return out
    boot = bootstrap_means(days, values, seed=20261123)
    lo, hi = float(np.percentile(boot, 1.25)), float(np.percentile(boot, 98.75))
    ds = sorted(set(days))
    cut = ds[len(ds) // 2]
    first = [v for d, v in zip(days, values, strict=True) if d < cut]
    second = [v for d, v in zip(days, values, strict=True) if d >= cut]
    halves = bool(first and second and np.mean(first) > 0 and np.mean(second) > 0)
    out.update({"ci975": [round(lo, 2), round(hi, 2)], "halves_positive": halves, "stage":
                "FINAL" if n >= final else "FIRST_LOOK", "status": "PASS" if lo > 0 and halves else "FAIL"})
    return out


def report(d2_rows: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-session table (current ORB, A, A - ORB, B), totals and the pre-registered evaluation."""
    ev = VARIANTS_V1["evaluation"]
    orb = [r for r in d2_rows if r.get("strategy_id") == "orb_options"]
    a = [r for r in rows if r["variant"] == A_NAME]
    b = [r for r in rows if r["variant"] == B_NAME]

    def day_of(r: Mapping[str, Any]) -> date:
        return date.fromisoformat(str(r["day"])) if "day" in r else datetime.fromisoformat(
            str(r["generated_at"])).date()

    filled = [r for r in orb if r.get("status") == FILLED]
    paired = [r for r in a if r["status"] == FILLED and r.get("base_status") == FILLED]
    days = sorted({day_of(r) for r in [*orb, *a, *b]})
    table = []
    for d in days:
        def pick(xs: Sequence[Mapping[str, Any]], d: date = d) -> list[Mapping[str, Any]]:
            return [r for r in xs if day_of(r) == d]
        fo, fa, pb = pick(filled), pick([r for r in a if r["status"] == FILLED]), pick(paired)
        fb = pick([r for r in b if r["status"] == FILLED])
        table.append({"day": d.isoformat(), "before_registration": d < EVALUATION_FROM,
                      "orb": {"n": len(fo), "net": round(sum(float(r["net"]) for r in fo), 2)},
                      "A": {"n": len(fa), "net": round(sum(float(r["net"]) for r in fa), 2)},
                      "A_minus_orb": {"n": len(pb), "net": round(sum(float(r["net"]) - float(r["base_net"])
                                                                        for r in pb), 2)},
                      "B": {"n": len(fb), "net": round(sum(float(r["net"]) for r in fb), 2),
                            "signals": len(pick(b))}})
    counted_pairs = [r for r in paired if day_of(r) >= EVALUATION_FROM]
    counted_b = [r for r in b if r["status"] == FILLED and day_of(r) >= EVALUATION_FROM]
    tests = {
        "A": _test([day_of(r) for r in counted_pairs], [float(r["net"]) - float(r["base_net"]) for r in counted_pairs],
                   int(ev["first_look"]["A"]), int(ev["final"]["A"])),
        "B": _test([day_of(r) for r in counted_b], [float(r["net"]) for r in counted_b],
                   int(ev["first_look"]["B"]), int(ev["final"]["B"])),
    }

    def totals(xs: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], float]) -> dict[str, Any]:
        cnt = [r for r in xs if day_of(r) >= EVALUATION_FROM]
        return {"n": len(cnt), "net": round(sum(key(r) for r in cnt), 2)}

    def coverage(xs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in xs:
            out[str(r["status"])] = out.get(str(r["status"]), 0) + 1
        return out

    return {"version": VARIANTS_VERSION, "evaluation_from": EVALUATION_FROM.isoformat(), "sessions": table,
            "since_registration": {"orb": totals(filled, lambda r: float(r["net"])),
                                   "A": totals([r for r in a if r["status"] == FILLED], lambda r: float(r["net"])),
                                   "A_minus_orb": totals(paired, lambda r: float(r["net"]) - float(r["base_net"])),
                                   "B": totals([r for r in b if r["status"] == FILLED], lambda r: float(r["net"]))},
            "coverage": {"orb": coverage(orb), "A": coverage(a), "B": coverage(b)}, "evaluation": tests}


def _rs(x: float) -> str:
    return f"{x:+,.0f}"


def render_text(rep: Mapping[str, Any]) -> list[str]:
    lines = [f"Forward shadow variants {rep['version']} next to the current ORB (recorded quotes, 1 lot, after "
             f"charges; counted from {rep['evaluation_from']}; * = before registration, never counted)",
             f"  {'session':12} {'ORB now':>16} {'A: their exit':>16} {'A - ORB':>16} {'B: 30m retest':>16}"]
    for s in rep["sessions"]:
        cells = [f"{s[k]['n']} | {_rs(s[k]['net'])}" for k in ("orb", "A", "A_minus_orb", "B")]
        lines.append(f"  {s['day'] + (' *' if s['before_registration'] else ''):12} "
                     + " ".join(f"{c:>16}" for c in cells))
    t = rep["since_registration"]
    lines.append(f"  {'counted':12} " + " ".join(f"{str(t[k]['n']) + ' | ' + _rs(t[k]['net']):>16}"
                                                 for k in ("orb", "A", "A_minus_orb", "B")))
    lines.append(f"  coverage: {rep['coverage']}")
    for k, e in rep["evaluation"].items():
        what = "A - current ORB per signal" if k == "A" else "B net per trade"
        if e["status"] == "NOT_YET_EVALUABLE":
            lines.append(f"  {k} ({what}): not yet evaluable, {e['n']} of {e['first_look_at']} trades for the first "
                         f"look ({100 * e['progress']:.1f} %), mean so far {e['mean']}")
        else:
            lines.append(f"  {k} ({what}): {e['stage']} {e['status']}: n {e['n']}, mean {e['mean']}, 97.5 % "
                         f"interval {e['ci975']}, both halves positive {e['halves_positive']}")
    return lines


__all__ = ["A_NAME", "B_NAME", "NO_BASE", "NO_RANGE", "Trade", "VariantInputs", "enter",
           "orb_range", "render_text", "report", "retest_signal", "run_trade", "stop_trigger", "variant_a",
           "variant_b"]
