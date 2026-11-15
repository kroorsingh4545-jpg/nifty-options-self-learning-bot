"""Counterfactual simulation of one signal on recorded quotes (pure).

The question: had this signal been traded alone, with the bot's own rules,
what would the trade have done? Nothing here can affect trading: the inputs
are recorded quotes and plain rule values, the output is a result object.

The model follows the bot step by step:

1. **Contract** (``runtime.contracts.OptionUniverse.select``): nearest expiry
   on or after the signal day; ATM = the listed strike nearest
   ``round(spot / step) * step``; ITM / OTM offsets as configured.
2. **Decision quote** (``RiskManager.evaluate_entry``): the latest recorded
   quote at the signal time, at most ``max_quote_age_seconds`` old. With
   5-second sampling a recorded quote can be older than the live tick the bot
   used; then the first quote within ``entry_timeout_seconds`` is used and the
   result is flagged ``late_decision_quote``. The bot refuses a quote without
   both bid and ask (``NO_TWO_SIDED``). Stop and target come from the ASK.
3. **Entry fill** (``PaperBroker``): a LIMIT at ask + ``entry_buffer_ticks``,
   filled at ask + ``slippage_ticks`` (rounded up), never above the limit,
   only while the top-of-book ask quantity covers the order (partial fills
   on), within ``entry_timeout_seconds``; otherwise ``NO_FILL``.
4. **Exit** (``PositionManager.on_tick``): on every later quote the trailing
   stop is updated first, then the exit trigger is checked on the LAST price.
   Stops fill at the worse of bid and last, targets at the bid, both minus
   ``slippage_ticks`` (rounded down). Square-off at the first quote at or after
   ``square_off_start``. No quote left: ``END_OF_DATA`` at the last quote.

**Sampling band.** Quotes are recorded every 5 s (1 s while the bot holds the
contract), but the bot reacts to every tick. A stop or target is usually
crossed BETWEEN two recorded quotes, so the real fill lies between the trigger
level and the price at the next recorded quote. The stored exit is the
CONSERVATIVE end of that band (the recorded quote for a stop, the trigger level
for a target); ``exit_price_best`` is the other end. Calibration checks that
real fills fall inside the band.

**Replay.** For calibration only, ``decision_ask`` replays the exact ask the
risk manager logged at the signal time, so errors in the trade rules can be
told apart from errors due to sampling.

Not simulated (flagged where they apply): daily loss limits, cooldowns and the
kill switch (a counterfactual trade stands alone), STRUCTURAL trailing (an
underlying level), the RL trade manager, multi-lot partial fills.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from research.shadow.rules import (
    END_OF_DATA,
    SQUARE_OFF,
    STOP_LOSS,
    TARGET,
    TRAILING_STOP,
    Levels,
    TradeRules,
    Trail,
    check_exit,
    levels,
)

#: result statuses; only FILLED has a trade
FILLED, NO_CONTRACT, NO_SPOT, NO_QUOTE, NO_TWO_SIDED, BAD_LEVELS, NO_FILL = (
    "FILLED", "NO_CONTRACT", "NO_SPOT", "NO_QUOTE", "NO_TWO_SIDED", "BAD_LEVELS", "NO_FILL")
STATUSES = (FILLED, NO_CONTRACT, NO_SPOT, NO_QUOTE, NO_TWO_SIDED, BAD_LEVELS, NO_FILL)


@dataclass(frozen=True)
class Contract:
    instrument_key: str
    option_type: str  # CE | PE
    strike: float
    expiry: date
    lot_size: int


@dataclass(frozen=True)
class Quote:
    received: datetime
    exchange: datetime
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: float | None = None


@dataclass
class SimResult:
    status: str
    flags: list[str] = field(default_factory=list)
    liquidity: list[str] = field(default_factory=list)  # what the resolver's liquidity filter would refuse
    decision_quote_at: datetime | None = None
    entry_ref: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_per_unit: float | None = None
    entry_time: datetime | None = None
    entry_price: float | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_price_best: float | None = None  # the optimistic end of the sampling band (see module doc)
    final_stop: float | None = None
    quantity: int = 0
    gross: float | None = None
    charges: float | None = None
    net: float | None = None
    r_gross: float | None = None  # (exit - entry) / risk per unit, as the bot records it
    r_net: float | None = None  # net / (risk per unit x quantity)
    mfe_r: float | None = None  # best last price after entry, in R
    mae_r: float | None = None  # worst last price after entry, in R
    quotes_used: int = 0
    max_gap_seconds: float | None = None


def select_contract(spot: float, option_type: str, day: date, contracts: Iterable[Contract], *, mode: str = "ATM",
                    offset: int = 0, step: int = 50) -> Contract | None:
    """``OptionUniverse.select`` on the nearest expiry on or after ``day``."""
    opts = [c for c in contracts if c.expiry >= day]
    if not opts or spot <= 0:
        return None
    expiry = min(c.expiry for c in opts)
    by_strike = {(c.strike, c.option_type): c for c in opts if c.expiry == expiry}
    strikes = sorted({s for s, _ in by_strike})
    target = round(spot / step) * step
    atm = min(strikes, key=lambda s: (abs(s - target), s))
    if mode == "ATM" or offset == 0:
        strike = atm
    else:
        direction = -1 if (mode == "ITM") == (option_type == "CE") else 1
        strike = atm + direction * offset * step
    return by_strike.get((strike, option_type))


def _buy_price(ref: float, r: TradeRules) -> float:
    steps = math.ceil((ref + r.slippage_ticks * r.tick_size) / r.tick_size - 1e-9)
    return round(max(r.tick_size, steps * r.tick_size), 2)


def _sell_price(ref: float, r: TradeRules) -> float:
    steps = math.floor((ref - r.slippage_ticks * r.tick_size) / r.tick_size + 1e-9)
    return round(max(r.tick_size, steps * r.tick_size), 2)


def liquidity_reasons(q: Quote, r: TradeRules) -> list[str]:
    """``data.option_chain.LiquidityFilter.check`` on the decision quote."""
    out = []
    if (q.volume or 0) < r.min_volume:
        out.append(f"volume {q.volume or 0} < {r.min_volume}")
    if (q.oi or 0.0) < r.min_oi:
        out.append(f"oi {q.oi or 0:.0f} < {r.min_oi:.0f}")
    if not q.bid or not q.ask or q.ask < q.bid:
        out.append("no valid two-sided quote")
    else:
        mid = (q.bid + q.ask) / 2
        spread_pct = 100 * (q.ask - q.bid) / mid
        if spread_pct > r.max_spread_pct:
            out.append(f"spread {spread_pct:.2f}% > {r.max_spread_pct}%")
    if q.ltp <= 0:
        out.append("no last traded price")
    elif q.ltp < r.min_premium:
        out.append(f"premium {q.ltp} < {r.min_premium}")
    elif r.max_premium is not None and q.ltp > r.max_premium:
        out.append(f"premium {q.ltp} > {r.max_premium}")
    return out


def _decision_quote(quotes: Sequence[Quote], at: datetime, r: TradeRules) -> tuple[int, bool] | None:
    """Index of the quote the risk manager would have seen, and whether it is a late one."""
    before = [i for i, q in enumerate(quotes) if q.received <= at]
    if before and (at - quotes[before[-1]].received).total_seconds() <= r.max_quote_age_seconds:
        return before[-1], False
    limit = at + timedelta(seconds=r.entry_timeout_seconds)
    after = next((i for i, q in enumerate(quotes) if at < q.received <= limit), None)
    return None if after is None else (after, True)


def simulate(decision_at: datetime, quotes: Sequence[Quote], r: TradeRules, *, lot_size: int,
             cost_fn: Callable[[int, float, float], float], lots: int = 1,
             decision_ask: float | None = None) -> SimResult:
    """One counterfactual long-option trade of ``lots`` lots. ``quotes`` are
    the contract's recorded quotes in receipt order; only quotes from the
    decision on are used for the trade (the decision quote may be up to
    ``max_quote_age_seconds`` older than the signal). ``decision_ask``
    (calibration replay only) is the ask the risk manager logged: it becomes
    the decision quote and fills the entry, without depth."""
    recorded = list(quotes)
    if decision_ask is not None:  # appended last: a recorded quote at the same instant sorts before it
        recorded.append(Quote(decision_at, decision_at, decision_ask, decision_ask, decision_ask))
    quotes = sorted(recorded, key=lambda q: q.received)
    found = _decision_quote(quotes, decision_at, r)
    if found is None:
        return SimResult(NO_QUOTE)
    i0, late = found
    dq = quotes[i0]
    res = SimResult(NO_FILL, decision_quote_at=dq.received, liquidity=liquidity_reasons(dq, r))
    if late:
        res.flags.append("late_decision_quote")
    if decision_ask is not None:
        res.flags.append("replayed_decision_quote")
    if r.trailing_enabled and r.trailing_mode == "STRUCTURAL":
        res.flags.append("structural_trailing_not_simulated")
    if dq.bid is None or dq.ask is None or not dq.bid or not dq.ask:
        res.status = NO_TWO_SIDED
        return res
    lv: Levels | None = levels(dq.ask, r)
    if lv is None:
        res.status = BAD_LEVELS
        return res
    res.entry_ref, res.stop, res.target, res.risk_per_unit = lv.entry, lv.stop, lv.target, round(lv.risk_per_unit, 4)
    qty = lots * lot_size
    limit = round(math.ceil((dq.ask + r.entry_buffer_ticks * r.tick_size) / r.tick_size - 1e-9) * r.tick_size, 2)
    deadline = dq.received + timedelta(seconds=r.entry_timeout_seconds)
    entry_i = None
    for i in range(i0, len(quotes)):
        q = quotes[i]
        if q.received > deadline:
            break
        ref = q.ask if q.ask else q.ltp
        if ref > limit or (q.ask_qty and q.ask_qty < qty):
            continue
        entry_i, res.entry_price, res.entry_time = i, min(_buy_price(ref, r), limit), q.received
        break
    if entry_i is None:
        return res
    res.status, res.quantity = FILLED, qty
    assert res.entry_price is not None
    trail = Trail(r, lv)
    risk = lv.risk_per_unit
    best = worst = res.entry_price
    prev = quotes[entry_i].received
    gaps: list[float] = []
    exit_reason: str | None = None
    exit_q: Quote | None = None
    pending: str | None = None  # an exit triggered but not yet fillable (bid depth below the order)
    level: float | None = None  # the stop or target that triggered
    for q in quotes[entry_i + 1:]:
        gaps.append((q.received - prev).total_seconds())
        prev = q.received
        res.quotes_used += 1
        best, worst = max(best, q.ltp), min(worst, q.ltp)
        if pending is None:
            trail.update(q.ltp)
            hit = check_exit(q.ltp, trail.stop, lv.target)
            if hit == STOP_LOSS and trail.active:
                hit = TRAILING_STOP
            if hit is None and q.received.time() >= r.square_off_start:
                hit = SQUARE_OFF
            pending = hit
            level = trail.stop if hit in (STOP_LOSS, TRAILING_STOP) else (lv.target if hit == TARGET else None)
        if pending is not None:
            if q.bid_qty and q.bid_qty < qty:
                if "exit_waited_for_depth" not in res.flags:
                    res.flags.append("exit_waited_for_depth")
                continue
            exit_reason, exit_q = pending, q
            break
    if exit_q is None:
        exit_q = quotes[-1] if len(quotes) > entry_i + 1 else quotes[entry_i]
        exit_reason = pending or END_OF_DATA
        res.flags.append("ended_without_exit_trigger" if pending is None else "exit_never_filled")
    ref = exit_q.bid if exit_q.bid else exit_q.ltp
    if exit_reason in (STOP_LOSS, TRAILING_STOP):
        ref = min(ref, exit_q.ltp)
    sampled = _sell_price(ref, r)
    at_level = _sell_price(level, r) if level is not None else sampled
    if exit_reason == TARGET:  # never credit more than the target the bot would have sold at
        res.exit_price, res.exit_price_best = min(sampled, at_level), max(sampled, at_level)
    elif exit_reason in (STOP_LOSS, TRAILING_STOP):  # the recorded quote is the worse end
        res.exit_price, res.exit_price_best = sampled, max(sampled, at_level)
    else:
        res.exit_price = res.exit_price_best = sampled
    res.exit_time, res.exit_reason = exit_q.received, exit_reason
    res.final_stop = trail.stop
    res.gross = round((res.exit_price - res.entry_price) * qty, 2)
    res.charges = round(cost_fn(qty, res.entry_price, res.exit_price), 2)
    res.net = round(res.gross - res.charges, 2)
    res.r_gross = round((res.exit_price - res.entry_price) / risk, 3)
    res.r_net = round(res.net / (risk * qty), 3)
    res.mfe_r = round((best - res.entry_price) / risk, 3)
    res.mae_r = round((worst - res.entry_price) / risk, 3)
    res.max_gap_seconds = round(max(gaps), 1) if gaps else None
    return res


__all__ = ["BAD_LEVELS", "FILLED", "NO_CONTRACT", "NO_FILL", "NO_QUOTE", "NO_SPOT", "NO_TWO_SIDED", "STATUSES",
           "Contract", "Quote", "SimResult", "liquidity_reasons", "select_contract", "simulate"]
