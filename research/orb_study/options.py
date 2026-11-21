"""ESTIMATED option prices and option-structure trades for ORB study E1 (pure).

ESTIMATED, not real option prices: we have no historical option quotes, so
every premium here is a Black-Scholes value with India VIX as the IV (flat, no
skew) on the D3 business-time clock (``volatility.business_time``):

* at entry (bar ``m0``'s close) the total implied variance to the 15:30 expiry
  close is ``V0 = iv0**2 x calendar years``;
* each trading minute held uses up the business-time minute variance ``u``
  (overnight share ``w`` and weekend gap weights as in D3);
* a VIX move rescales what is left: ``V(m) = (iv_m / iv0)**2 x (V0 - u (m - m0))``.

Fills follow the backtest cost model: buy at the quote's ask plus the
slippage, sell at the bid minus it (``Fills``, built from
``backtest/cost_model.py`` by the composition root), plus the charges of every
order. A spread is four orders.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from research.orb_study.data import SQUARE_OFF_BAR, Session, bar_close
from research.orb_study.setups import Entry
from research.shadow.rules import TRAILING_STOP, Levels, TradeRules, Trail, levels
from volatility.business_time import business_minute_variance, clock_from_sessions
from volatility.options import CALENDAR_DAYS, greeks, price_var

LONG_ATM, DEBIT, CREDIT = "LONG_ATM", "DEBIT", "CREDIT"
STRUCTURES = (LONG_ATM, DEBIT, CREDIT)
MINUTES_PER_YEAR = CALENDAR_DAYS * 24 * 60
EXPIRY_CLOSE_MINUTES = 15 * 60 + 30


@dataclass(frozen=True)
class Market:
    """Calendar inputs (built by the composition root from the research store)."""

    expiry_of: Callable[[date], date | None]  # nearest weekly expiry on or after the day
    gap_weights: Callable[[date, date], list[float]]  # one weight per session after the day up to the expiry
    overnight_share: Callable[[date], float]
    r: float = 0.065
    q: float = 0.0
    strike_step: int = 50


@dataclass(frozen=True)
class Fills:
    """Execution costs (from ``backtest.cost_model.CostModel`` and the configured charges)."""

    quote: Callable[[float], tuple[float, float]]  # (bid, ask) around a mid price
    charges: Callable[[str, int, float], float]  # (BUY | SELL, quantity, price) -> rupees for one order
    slippage: float  # rupees per unit per fill (slippage ticks x tick)
    tick: float
    lot: int

    def buy(self, mid: float) -> float:
        ask = self.quote(max(mid, 0.0))[1]
        return round(math.ceil((ask + self.slippage) / self.tick - 1e-9) * self.tick, 2)

    def sell(self, mid: float) -> float:
        bid = self.quote(max(mid, 0.0))[0]
        return round(max(self.tick, math.floor((bid - self.slippage) / self.tick + 1e-9) * self.tick), 2)


class Pricer:
    """Prices options of one entry's expiry from the entry bar on (ESTIMATED)."""

    def __init__(self, s: Session, m0: int, mk: Market) -> None:
        self.s, self.m0, self.mk = s, m0, mk
        self.ok = False
        self.expiry = mk.expiry_of(s.day)
        vix0 = s.vix_at(m0)
        if self.expiry is None or vix0 is None or vix0 <= 0:
            return
        t0 = datetime.combine(s.day, bar_close(m0))
        close = datetime.combine(self.expiry, datetime.min.time()) + timedelta(minutes=EXPIRY_CLOSE_MINUTES)
        cal_minutes = (close - t0).total_seconds() / 60
        if cal_minutes <= 0:
            return
        weights = mk.gap_weights(s.day, self.expiry)
        self.dte = len(weights)  # trading sessions after today up to the expiry (0 = expiry day)
        self.years0 = cal_minutes / MINUTES_PER_YEAR
        minutes_left_today = EXPIRY_CLOSE_MINUTES - (t0.hour * 60 + t0.minute)
        clock = clock_from_sessions(self.years0, minutes_left_today, weights)
        self.iv0 = vix0 / 100.0
        self.v0 = self.iv0 * self.iv0 * self.years0
        self.u = business_minute_variance(self.iv0, clock, mk.overnight_share(s.day))
        self.ok = math.isfinite(self.u)

    def _var(self, m: int) -> tuple[float, float]:
        vix = self.s.vix_at(m) or self.iv0 * 100
        used = self.u * (m - self.m0)
        scale = (vix / 100.0 / self.iv0) ** 2
        return max(self.v0 - used, 0.0) * scale, max(self.years0 - (m - self.m0) / MINUTES_PER_YEAR, 1e-9)

    def value(self, m: int, spot: float, strike: float, call: bool) -> float:
        """Estimated premium at bar ``m``'s close with the index at ``spot``."""
        var, years = self._var(m)
        return price_var(spot, strike, var, years, self.mk.r, self.mk.q, call)

    def atm(self, spot: float) -> float:
        step = self.mk.strike_step
        return float(round(spot / step) * step)

    def abs_delta(self, spot: float, strike: float, call: bool) -> float:
        g = greeks(spot, strike, self.years0, self.iv0, self.mk.r, self.mk.q, call)
        return 0.0 if g is None else abs(g.delta)

    def delta_strike(self, spot: float, call: bool, target: float = 0.20, max_steps: int = 60) -> float:
        """The OTM strike (50-step) whose model |delta| is nearest ``target``; ties go further OTM."""
        step = self.mk.strike_step
        atm = self.atm(spot)
        best: tuple[float, int] | None = None
        for k in range(1, max_steps + 1):
            strike = atm + (k if call else -k) * step
            if strike <= 0:
                break
            err = abs(self.abs_delta(spot, strike, call) - target)
            if best is None or err <= best[0] + 1e-12:
                best = (err, k)
        assert best is not None
        return atm + (best[1] if call else -best[1]) * step


@dataclass(frozen=True)
class Leg:
    strike: float
    call: bool
    sign: int  # +1 long, -1 short


def legs_for(structure: str, e: Entry, p: Pricer) -> list[Leg]:
    up = e.direction > 0
    atm = p.atm(e.spot)
    if structure == LONG_ATM:
        return [Leg(atm, up, 1)]
    if structure == DEBIT:
        return [Leg(atm, up, 1), Leg(p.delta_strike(e.spot, up), up, -1)]
    if structure == CREDIT:  # sell ATM of the opposite type, buy its 0.2-delta protection
        return [Leg(atm, not up, -1), Leg(p.delta_strike(e.spot, not up), not up, 1)]
    raise ValueError(f"unknown structure {structure!r}")


@dataclass
class OptTrade:
    structure: str
    day: date
    direction: int
    dte: int
    net: float  # rupees per lot after every cost
    gross: float  # at estimated mid prices, before any cost
    costs: float  # gross - net (charges + spread + slippage)
    entry_cash: float  # rupees paid (+) or received (-) at entry, after fills
    max_loss: float  # rupees per lot if the structure expires worthless against us (estimated)
    margin_est: float  # rough capital blocked (see ``margin_estimate``)
    exit_m: int
    exit_reason: str
    info: dict[str, Any]


def margin_estimate(structure: str, legs: Sequence[Leg], mids0: Sequence[float], spot: float, lot: int) -> float:
    """ROUGH capital per lot: long = premium; debit spread = net debit; credit
    spread = its maximum loss + a 2 % exposure margin on the short leg's
    notional (NSE-style; not SPAN; the real broker margin is not queried)."""
    net = sum(leg.sign * m for leg, m in zip(legs, mids0, strict=True)) * lot
    if structure != CREDIT:
        return max(net, 0.0)
    width = abs(legs[0].strike - legs[1].strike)
    return max(width * lot + net, 0.0) + 0.02 * spot * lot


def value_trade(structure: str, e: Entry, p: Pricer, f: Fills, legs: Sequence[Leg], exit_m: int,
                exit_mids: Sequence[float], exit_reason: str, info: dict[str, Any] | None = None) -> OptTrade:
    mids0 = [p.value(e.m, e.spot, leg.strike, leg.call) for leg in legs]
    lot = f.lot
    cash = gross = charges = 0.0
    for leg, m0, m1 in zip(legs, mids0, exit_mids, strict=True):
        if leg.sign > 0:
            b, s_ = f.buy(m0), f.sell(m1)
            cash += (s_ - b) * lot
            charges += f.charges("BUY", lot, b) + f.charges("SELL", lot, s_)
        else:
            s_, b = f.sell(m0), f.buy(m1)
            cash += (s_ - b) * lot
            charges += f.charges("SELL", lot, s_) + f.charges("BUY", lot, b)
        gross += leg.sign * (m1 - m0) * lot
    entry_cash = sum((f.buy(m0) if leg.sign > 0 else -f.sell(m0)) for leg, m0 in zip(legs, mids0, strict=True)) * lot
    net = cash - charges
    width = abs(legs[0].strike - legs[1].strike) if len(legs) == 2 else 0.0
    max_loss = width * lot + entry_cash if structure == CREDIT else max(entry_cash, 0.0)
    return OptTrade(structure, e.day, e.direction, p.dte, round(net, 2), round(gross, 2), round(gross - net, 2),
                    round(entry_cash, 2), round(max_loss, 2),
                    round(margin_estimate(structure, legs, mids0, e.spot, lot), 2), exit_m, exit_reason,
                    {"strikes": [leg.strike for leg in legs], "entry_mids": [round(x, 2) for x in mids0],
                     "exit_mids": [round(x, 2) for x in exit_mids], **(info or {})})


def structure_trade(structure: str, s: Session, e: Entry, p: Pricer, f: Fills, exit_m: int, exit_spot: float,
                    exit_reason: str) -> OptTrade:
    """A structure entered at the entry bar and closed at ``exit_m`` with the index at ``exit_spot``."""
    legs = legs_for(structure, e, p)
    mids1 = [p.value(exit_m, exit_spot, leg.strike, leg.call) for leg in legs]
    return value_trade(structure, e, p, f, legs, exit_m, mids1, exit_reason)


def approx_long_atm(e: Entry, points: float, p: Pricer, f: Fills) -> dict[str, float]:
    """H3: long ATM approximated as 0.5 x spot points x lot; the estimated ATM
    premium at entry (and that premium + 0.5 x points at exit) is the cost basis."""
    lot = f.lot
    mid0 = p.value(e.m, e.spot, p.atm(e.spot), e.direction > 0)
    mid1 = max(f.tick, mid0 + 0.5 * points)
    b, s_ = f.buy(mid0), f.sell(mid1)
    charges = f.charges("BUY", lot, b) + f.charges("SELL", lot, s_)
    gross = 0.5 * points * lot
    costs = (b - mid0) * lot + (mid1 - s_) * lot + charges  # spread + slippage + charges
    net = gross - costs
    return {"gross": round(gross, 2), "net": round(net, 2), "costs": round(gross - net, 2), "premium": round(mid0, 2)}


def our_exit_long(s: Session, e: Entry, p: Pricer, f: Fills, rules: TradeRules, *,
                  exit_bar: int = SQUARE_OFF_BAR) -> OptTrade | str:
    """The bot's premium exit on an estimated long ATM option: stop / target
    from the entry ask (``research.shadow.rules.levels``), trailing stop,
    square-off at ``exit_bar``'s close. Each bar: stop on the adverse extreme,
    then target on the favourable extreme, then the trail moves (effective next
    bar); a level that the bar opened through fills at the open. Returns a
    refusal reason instead when the bot would not have traded."""
    if e.m >= exit_bar:
        return "AFTER_SQUARE_OFF"
    call = e.direction > 0
    strike = p.atm(e.spot)
    mid0 = p.value(e.m, e.spot, strike, call)
    if mid0 < rules.min_premium or (rules.max_premium is not None and mid0 > rules.max_premium):
        return "PREMIUM_BAND"
    ask0 = f.quote(mid0)[1]
    lv: Levels | None = levels(ask0, rules)
    if lv is None:
        return "BAD_LEVELS"
    trail = Trail(rules, lv)
    exit_m, exit_mid, reason = exit_bar, None, "SQUARE_OFF"
    for m in range(e.m + 1, exit_bar + 1):
        if not s.present(m):
            continue
        adv, fav = (s.lo[m], s.h[m]) if call else (s.h[m], s.lo[m])
        p_open = p.value(m, float(s.o[m]), strike, call)
        p_adv = p.value(m, float(adv), strike, call)
        p_fav = p.value(m, float(fav), strike, call)
        stop = trail.stop
        if p_adv <= stop or p_open <= stop:
            exit_m, exit_mid = m, min(p_open, stop)
            reason = TRAILING_STOP if trail.active else "STOP_LOSS"
            break
        if lv.target is not None and (p_fav >= lv.target or p_open >= lv.target):
            exit_m, exit_mid, reason = m, max(p_open, lv.target), "TARGET"
            break
        trail.update(p_fav)
        if m == exit_bar:
            exit_m, exit_mid = m, p.value(m, float(s.c[m]), strike, call)
    if exit_mid is None:  # the exit bar was missing: the last bar's close
        last = max((m for m in range(e.m, exit_bar + 1) if s.present(m)), default=e.m)
        exit_m, exit_mid = last, p.value(last, float(s.c[last]), strike, call)
    return value_trade(LONG_ATM, e, p, f, [Leg(strike, call, 1)], exit_m, [exit_mid], reason,
                       {"stop0": lv.stop, "target": lv.target, "final_stop": trail.stop})


__all__ = ["CREDIT", "DEBIT", "LONG_ATM", "STRUCTURES", "Fills", "Leg", "Market", "OptTrade", "Pricer",
           "approx_long_atm", "legs_for", "margin_estimate", "our_exit_long", "structure_trade", "value_trade"]
