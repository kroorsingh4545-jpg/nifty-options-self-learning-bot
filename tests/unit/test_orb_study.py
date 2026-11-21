"""ORB study E1: setups, spot exits, estimated option pricing, structures and the pass rule (synthetic data)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, time

import numpy as np
import pytest

from research.orb_study.data import BARS, LAST_BAR, SQUARE_OFF_BAR, Session, bar_index, bar_start, data_check
from research.orb_study.options import (
    CREDIT,
    DEBIT,
    LONG_ATM,
    Fills,
    Market,
    Pricer,
    approx_long_atm,
    legs_for,
    our_exit_long,
    structure_trade,
)
from research.orb_study.registration import STUDY_E1
from research.orb_study.setups import (
    BREAKOUT,
    RETEST,
    REVERSAL,
    STOP,
    TARGET,
    TIME,
    filtered,
    our_entries,
    source_entries,
    spot_exit,
)
from research.orb_study.stats import apply_bonferroni, max_drawdown, pass_rule, summary, top_share
from research.regime.orb import OrbRule
from research.shadow.rules import TradeRules
from volatility.options import price

DAY = date(2024, 3, 5)  # a Tuesday; expiry Thursday 2024-03-07


def session(closes: Sequence[float], *, day: date = DAY, vix: float = 14.0, spread: float = 0.0) -> Session:
    c = np.full(BARS, np.nan)
    c[:len(closes)] = closes
    o = np.concatenate([[c[0]], c[:-1]])
    h, lo = np.maximum(o, c) + spread, np.minimum(o, c) - spread
    return Session(day, o, h, lo, c, np.where(np.isfinite(c), vix, np.nan), vix)


def path(head: Sequence[float], fill: float) -> list[float]:
    return list(head) + [fill] * (BARS - len(head))


def market(expiry: date = date(2024, 3, 7)) -> Market:
    def gaps(day: date, exp: date) -> list[float]:
        return [1.0] * max((exp - day).days, 0)
    return Market(expiry_of=lambda d: expiry if d <= expiry else None, gap_weights=gaps,
                  overnight_share=lambda d: 0.35)


def fills(lot: int = 65) -> Fills:
    def quote(mid: float) -> tuple[float, float]:
        half = max(0.05, mid * 0.0025)
        bid = math.floor((mid - half) / 0.05 + 1e-9) * 0.05
        ask = math.ceil((mid + half) / 0.05 - 1e-9) * 0.05
        return round(max(0.05, bid), 2), round(max(ask, 0.10), 2)
    return Fills(quote=quote, charges=lambda side, q, p: 20.0, slippage=0.05, tick=0.05, lot=lot)


def rules(**kw: object) -> TradeRules:
    base: dict[str, object] = dict(stop_mode="PREMIUM_PERCENT", stop_value=25, target_mode="R_MULTIPLE",
                                   target_value=2.0, trailing_enabled=True, trailing_mode="R_MULTIPLE",
                                   trailing_activation=1.0, trailing_step=0.5, min_premium=0.0, max_premium=None)
    base.update(kw)
    return TradeRules(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- setups
def test_breakout_retest_and_reversal_follow_the_registered_rules() -> None:
    # range bars 0..4 at 100 with h/l +-1 -> hi 101, lo 99 (spread 1)
    head = [100] * 5 + [100.5, 102, 103, 100, 102.5, 98, 100]
    s = session(path(head, 100), spread=0.0)
    s.h[:5], s.lo[:5] = 101, 99
    e = source_entries(s, 5)
    b, rt, rv = e[BREAKOUT], e[RETEST], e[REVERSAL]
    assert b is not None and b.m == 6 and b.direction == 1 and b.spot == 102 and (b.hi, b.lo) == (101, 99)
    assert rt is not None and rt.m == 9 and rt.direction == 1  # back inside at 8, beyond the same side at 9
    assert rv is not None and rv.m == 10 and rv.direction == -1 and rv.first_break_m == 6
    x = spot_exit(s, b)
    assert x is not None and x.reason == STOP and x.m == 10 and x.points(b) == pytest.approx(98 - 102)
    x = spot_exit(s, rv)  # short from 98, never closes above 101 again -> time exit 15:29
    assert x is not None and x.reason == TIME and x.m == LAST_BAR and x.points(rv) == pytest.approx(-2)


def test_no_entry_on_the_exit_bar_and_a_missing_range_minute_skips_the_day() -> None:
    s = session(path([100] * 5, 100))
    s.h[:5], s.lo[:5] = 101, 99
    s.c[LAST_BAR] = 105  # breaks only on the 15:29 bar
    assert source_entries(s, 5)[BREAKOUT] is None
    s2 = session(path([100] * 5 + [103], 103))
    s2.c[2] = np.nan
    assert all(v is None for v in source_entries(s2, 5).values())


def test_retest_needs_a_close_inside_first_and_reversal_does_not() -> None:
    s = session(path([100] * 5 + [102, 103, 98], 98))
    s.h[:5], s.lo[:5] = 101, 99
    e = source_entries(s, 5)
    assert e[RETEST] is None and e[REVERSAL] is not None and e[REVERSAL].m == 7


def test_square_off_exit_and_targets_fill_at_the_level_with_stop_first_in_the_same_bar() -> None:
    s = session(path([100] * 5 + [102], 102))
    s.h[:5], s.lo[:5] = 101, 99
    b = source_entries(s, 5)[BREAKOUT]
    assert b is not None and b.risk_points == pytest.approx(3)  # 102 - 99
    x = spot_exit(s, b, exit_bar=SQUARE_OFF_BAR)
    assert x is not None and x.m == SQUARE_OFF_BAR and x.reason == TIME
    s.h[50] = 108.5  # touches 2R = 108
    x = spot_exit(s, b, target_r=2.0)
    assert x is not None and x.reason == TARGET and x.spot == pytest.approx(108) and x.m == 50
    s.c[50] = 98  # the same bar also closes below the low: the stop wins
    x = spot_exit(s, b, target_r=2.0)
    assert x is not None and x.reason == STOP and x.spot == 98


def test_filters_use_open_percent_entry_cutoff_and_retest_delay() -> None:
    s = session(path([20000] * 60, 20000))
    s.o[0] = 20000
    s.h[:60], s.lo[:60] = 20060, 19940  # width 120 = 0.6 % of open
    s.c[bar_index(time(11, 58))] = 20070  # closes 11:59: before 12:00
    b = source_entries(s, 60)[BREAKOUT]
    assert b is not None and b.range_pct_of_open == pytest.approx(0.6)
    rule_a = STUDY_E1["filters_source"]["A_BREAKOUT_60"]
    assert filtered(b, rule_a)
    s.c[bar_index(time(11, 58))] = 20000
    s.c[bar_index(time(11, 59))] = 20070  # closes exactly 12:00: not before
    b2 = source_entries(s, 60)[BREAKOUT]
    assert b2 is not None and not filtered(b2, rule_a)
    rule_b = STUDY_E1["filters_source"]["B_RETEST_30"]
    assert not filtered(b2, rule_b)  # wrong setup
    from dataclasses import replace
    rt = replace(b2, setup=RETEST, range_minutes=30, first_break_m=b2.m - 61)
    assert not filtered(rt, rule_b)  # range 0.6 % > 0.5 % and 61 min late
    rt_ok = replace(rt, hi=20040, lo=19960, first_break_m=rt.m - 60)
    assert filtered(rt_ok, rule_b)


def test_our_entries_map_the_5_minute_candle_to_its_last_minute() -> None:
    closes = [20000.0] * 15 + [20000.0] * 4 + [20040.0]  # bars 15..19 form the 09:30-09:35 candle
    s = session(path(closes, 20040))
    s.h[:15], s.lo[:15] = 20020, 19980
    ents = our_entries(s, OrbRule(), tz=None)
    assert len(ents) == 1 and ents[0].m == 19 and ents[0].direction == 1 and ents[0].spot == 20040
    assert (ents[0].hi, ents[0].lo, ents[0].range_minutes) == (20020, 19980, 15)
    assert bar_start(19) == time(9, 34)


# ---------------------------------------------------------------- data check
def test_data_check_lists_exclusions_and_missing_minutes() -> None:
    full = {bar_start(m): (1.0, 1.0, 1.0, 1.0) for m in range(BARS)}
    gappy = {k: v for k, v in full.items() if k != time(10, 0)}
    late = {k: v for k, v in full.items() if k >= time(9, 16)}
    short = {bar_start(m): (1.0, 1.0, 1.0, 1.0) for m in range(60)}
    rep = data_check({date(2024, 1, 1): full, date(2024, 1, 2): gappy, date(2024, 1, 3): late,
                      date(2024, 1, 4): short}, {}, min_bars=370)
    assert rep["sessions_used"] == 2 and rep["missing_minutes_total"] == 1
    assert {x["day"] for x in rep["excluded"]} == {"2024-01-03", "2024-01-04"}
    assert rep["vix_missing_minutes_total"] == 2 * BARS


# ---------------------------------------------------------------- estimated options
def test_pricer_matches_black_scholes_at_entry_and_decays_on_the_business_clock() -> None:
    s = session(path([22000.0] * 30, 22000.0), vix=15.0)
    p = Pricer(s, 29, market())
    assert p.ok and p.dte == 2
    v0 = p.value(29, 22000, 22000, True)
    assert v0 == pytest.approx(price(22000, 22000, p.years0, 0.15, 0.065, 0.0, True), rel=1e-9)
    later = p.value(29 + 120, 22000, 22000, True)
    assert later < v0  # time decay
    calendar_only = price(22000, 22000, p.years0 - 120 / 525600, 0.15, 0.065, 0.0, True)
    assert later < calendar_only  # business time decays faster intraday than the calendar clock
    s.vix[100:] = 30.0
    assert p.value(150, 22000, 22000, True) > later  # VIX up -> remaining variance up


def test_expiry_day_variance_is_only_the_rest_of_the_session() -> None:
    s = session(path([22000.0] * 200, 22000.0), day=date(2024, 3, 7), vix=15.0)
    p = Pricer(s, 100, market())
    assert p.ok and p.dte == 0
    assert p.value(LAST_BAR, 22000, 22000, True) < 0.25 * p.value(100, 22000, 22000, True)


def test_the_wing_strike_is_near_0_2_delta_and_structures_have_the_registered_legs() -> None:
    s = session(path([22010.0] * 30, 22010.0), vix=14.0)
    p = Pricer(s, 29, market())
    k_call, k_put = p.delta_strike(22010, True), p.delta_strike(22010, False)
    assert k_call > 22000 and k_put < 22000
    assert abs(p.abs_delta(22010, k_call, True) - 0.2) < 0.06
    assert abs(p.abs_delta(22010, k_put, False) - 0.2) < 0.06
    up = source_like(22010, 1)
    assert [(lg.strike, lg.call, lg.sign) for lg in legs_for(LONG_ATM, up, p)] == [(22000, True, 1)]
    assert [(lg.call, lg.sign) for lg in legs_for(DEBIT, up, p)] == [(True, 1), (True, -1)]
    credit = legs_for(CREDIT, up, p)  # bullish = put credit spread
    assert [(lg.strike, lg.call, lg.sign) for lg in credit] == [(22000, False, -1), (k_put, False, 1)]
    down = source_like(22010, -1)
    assert [(lg.call, lg.sign) for lg in legs_for(CREDIT, down, p)] == [(True, -1), (True, 1)]


def source_like(spot: float, direction: int, m: int = 29) -> object:
    from research.orb_study.setups import Entry
    return Entry(BREAKOUT, 15, DAY, m, direction, spot, spot + 50, spot - 50, spot)


def test_structure_costs_four_orders_and_a_credit_spread_gains_when_price_moves_its_way() -> None:
    s = session(path([22000.0] * 30, 22000.0), vix=14.0)
    p = Pricer(s, 29, market())
    f = fills()
    e = source_like(22000, 1)
    flat_long = structure_trade(LONG_ATM, s, e, p, f, 30, 22000, TIME)
    flat_credit = structure_trade(CREDIT, s, e, p, f, 30, 22000, TIME)
    assert flat_long.net < 0 and flat_credit.net < 0
    assert flat_credit.costs > flat_long.costs  # four orders, two spreads
    assert flat_credit.costs >= 4 * 20.0
    assert flat_credit.entry_cash < 0 and 0 < flat_credit.max_loss < 65 * 1000
    win = structure_trade(CREDIT, s, e, p, f, 200, 22150, TIME)
    lose = structure_trade(CREDIT, s, e, p, f, 200, 21850, TIME)
    assert win.net > 0 > lose.net
    assert lose.net >= -flat_credit.max_loss - 200  # never much worse than its maximum loss
    assert flat_credit.margin_est > flat_credit.max_loss  # + exposure margin


def test_approx_long_atm_is_half_delta_points_minus_costs() -> None:
    s = session(path([22000.0] * 30, 22000.0), vix=14.0)
    p = Pricer(s, 29, market())
    a = approx_long_atm(source_like(22000, 1), 40.0, p, fills())
    assert a["gross"] == pytest.approx(0.5 * 40 * 65)
    assert a["net"] < a["gross"] and a["costs"] == pytest.approx(a["gross"] - a["net"])
    assert a["costs"] >= 40.0  # two orders' charges at least


def test_our_exit_long_stop_target_square_off_and_premium_band() -> None:
    f, mk = fills(), market()
    base = session(path([22000.0] * 30, 22000.0), vix=14.0)
    p = Pricer(base, 29, mk)
    e = source_like(22000, 1)
    t = our_exit_long(base, e, p, f, rules(trailing_enabled=False))
    assert not isinstance(t, str) and t.exit_reason == "SQUARE_OFF" and t.exit_m == SQUARE_OFF_BAR
    down = session(path([22000.0] * 30 + [21850.0], 21850.0), vix=14.0)
    t = our_exit_long(down, e, Pricer(down, 29, mk), f, rules())
    assert not isinstance(t, str) and t.exit_reason == "STOP_LOSS" and t.exit_m == 30
    stop0 = t.info["stop0"]
    assert t.info["exit_mids"][0] <= stop0 + 1e-9  # gapped through: filled at the (lower) open-bar price
    up = session(path([22000.0] * 30 + [22400.0], 22400.0), vix=14.0)
    t = our_exit_long(up, e, Pricer(up, 29, mk), f, rules())
    assert not isinstance(t, str) and t.exit_reason == "TARGET" and t.net > 0
    assert our_exit_long(base, e, p, f, rules(min_premium=500.0)) == "PREMIUM_BAND"
    late = source_like(22000, 1, m=SQUARE_OFF_BAR)
    assert our_exit_long(base, late, Pricer(base, SQUARE_OFF_BAR, mk), f, rules()) == "AFTER_SQUARE_OFF"


def test_trailing_stop_exits_after_a_rise_and_fall() -> None:
    f, mk = fills(), market()
    closes = [22000.0] * 30 + [22000.0 + 4 * k for k in range(1, 21)] + [22080.0 - 6 * k for k in range(1, 30)]
    s = session(path(closes, closes[-1]), vix=14.0)
    t = our_exit_long(s, source_like(22000, 1), Pricer(s, 29, mk), f, rules(target_value=10.0))
    assert not isinstance(t, str) and t.exit_reason == "TRAILING_STOP"
    assert t.info["final_stop"] > t.info["stop0"]


# ---------------------------------------------------------------- statistics
def test_pass_rule_years_bootstrap_and_bonferroni() -> None:
    days = [date(y, 1, d) for y in (2022, 2023, 2024, 2025) for d in range(1, 29)]
    good = [100.0 + (i % 7) for i in range(len(days))]
    t = pass_rule(days, good, draws=2000)
    assert t["passed"] and t["positive_years"] == 4 and t["ci95"][0] > 0
    bad_years = [(-50.0 if d.year in (2023, 2024) else 300.0) for d in days]
    t2 = pass_rule(days, bad_years, draws=2000)
    assert t2["mean"] > 0 and not t2["years_ok"] and not t2["passed"]
    noisy = [(5.0 if i % 2 else -4.0) + (i % 3) for i in range(len(days))]
    tests = {"a": t, "b": t2, "c": pass_rule(days, noisy, draws=2000)}
    assert apply_bonferroni(tests) == 3
    assert tests["a"]["verdict"] == "PASS" and tests["b"]["verdict"] == "FAIL"
    assert "_boot" not in tests["a"]
    again = pass_rule(days, good, draws=2000)
    assert again["ci95"] == pass_rule(days, good, draws=2000)["ci95"]  # seeded


def test_summary_drawdown_and_top_day_share() -> None:
    days = [date(2024, 1, d) for d in range(1, 11)]
    vals = [100, -50, -50, 300, 0, 0, 0, 0, 0, 0]
    s = summary(days, vals)
    assert s["trades"] == 10 and s["total"] == 300 and s["win_rate"] == 20.0
    assert s["profit_factor"] == pytest.approx(4.0) and s["max_drawdown"] == 100
    assert top_share(np.asarray(vals, dtype=float)) == pytest.approx(1.0)
    assert max_drawdown(np.asarray([-10.0, 5.0])) == 10


# ---------------------------------------------------------------- composition root
def test_cli_fills_use_the_backtest_cost_model_and_configured_charges() -> None:
    from backtest.cost_model import CostModel
    from config.loader import load_config
    from research.__main__ import orb_study_fills

    cfg = load_config(use_default_env_file=False)
    f = orb_study_fills(cfg, 0.05, 65)
    cm = CostModel(cfg.settings.costs, slippage_ticks=cfg.settings.paper_broker.slippage_ticks)
    assert f.quote(100.0) == cm.quote(100.0, 0.05) and f.lot == 65
    assert f.buy(100.0) >= cm.quote(100.0, 0.05)[1] and f.sell(100.0) <= cm.quote(100.0, 0.05)[0]
    buy, sell = f.charges("BUY", 65, 100.0), f.charges("SELL", 65, 100.0)
    assert sell > buy > cfg.settings.costs.brokerage_per_order  # STT on the sell side; GST on top of brokerage
