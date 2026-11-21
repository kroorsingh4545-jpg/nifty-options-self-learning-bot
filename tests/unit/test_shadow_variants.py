"""Forward shadow variants A and B (sv.v1): entry parity with D2, index-driven exits, rows, store and report."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from core.clock import IST
from research.orb_study.data import bar_start
from research.shadow.rules import TradeRules
from research.shadow.simulate import FILLED, NO_QUOTE, NO_TWO_SIDED, Contract, Quote, simulate
from research.shadow.variants import (
    A_NAME,
    B_NAME,
    NO_BASE,
    NO_RANGE,
    VariantInputs,
    _test,
    enter,
    orb_range,
    render_text,
    report,
    retest_signal,
    run_trade,
    stop_trigger,
    variant_a,
    variant_b,
)
from research.store import VARIANT_COLUMNS, ResearchStore, ResearchStoreError

DAY = date(2026, 12, 1)  # after registration (a Tuesday)


def at(hh: int, mm: int, ss: float = 0.0) -> datetime:
    return datetime.combine(DAY, time(hh, mm), tzinfo=IST) + timedelta(seconds=ss)


def rules(**kw: object) -> TradeRules:
    base: dict[str, object] = dict(stop_mode="PREMIUM_PERCENT", stop_value=25, target_mode="R_MULTIPLE",
                                   target_value=2.0, trailing_enabled=True, trailing_mode="R_MULTIPLE",
                                   trailing_activation=1.0, trailing_step=0.5, square_off_start=time(15, 10))
    base.update(kw)
    return TradeRules(**base)  # type: ignore[arg-type]


def q(t: datetime, ltp: float, *, spread: float = 0.5, qty: int = 1000) -> Quote:
    return Quote(t, t, ltp, ltp - spread / 2, ltp + spread / 2, qty, qty)


def flat_quotes(start: datetime, end: datetime, price: float = 100.0, step_s: int = 5) -> list[Quote]:
    out, t = [], start
    while t <= end:
        out.append(q(t, price))
        t += timedelta(seconds=step_s)
    return out


def bars_from(closes: dict[time, float], base: float = 20000.0, until: time = time(15, 30)) -> dict[time, tuple]:
    """Full session 09:15..15:29 at ``base`` with chosen closes; o = h = l = c unless set."""
    out = {}
    m = 0
    while bar_start(m) < until:
        c = closes.get(bar_start(m), base)
        out[bar_start(m)] = (c, c, c, c)
        m += 1
    return out


# ---------------------------------------------------------------- entry parity with D2
@pytest.mark.parametrize("case", ["normal", "late", "no_quote", "one_sided", "thin"])
def test_entry_matches_the_d2_simulator(case: str) -> None:
    r = rules()
    sig = at(9, 35, 1)
    quotes = flat_quotes(at(9, 34), at(9, 40))
    if case == "late":
        quotes = flat_quotes(at(9, 35, 20), at(9, 40))
    elif case == "no_quote":
        quotes = flat_quotes(at(9, 50), at(9, 55))
    elif case == "one_sided":
        quotes = [Quote(x.received, x.exchange, x.ltp, None, x.ask, None, 1000) for x in quotes]
    elif case == "thin":
        quotes = [Quote(x.received, x.exchange, x.ltp, x.bid, x.ask, 1000, 10) for x in quotes]
    d2 = simulate(sig, quotes, r, lot_size=65, cost_fn=lambda n, a, b: 40.0)
    mine, _ = enter(sig, sorted(quotes, key=lambda x: x.received), r, 65)
    assert (mine.status, mine.entry_price, mine.entry_time) == (d2.status, d2.entry_price, d2.entry_time)
    assert ("late_decision_quote" in mine.flags) == ("late_decision_quote" in d2.flags)
    if case == "no_quote":
        assert mine.status == NO_QUOTE
    if case == "one_sided":
        assert mine.status == NO_TWO_SIDED


# ---------------------------------------------------------------- index-driven exit
def test_stop_trigger_uses_closes_after_entry_before_square_off_with_the_delay() -> None:
    bars = bars_from({time(9, 40): 19900.0, time(10, 5): 19900.0, time(15, 10): 19000.0})
    kw = dict(hi=20050.0, lo=19950.0, call=True, square_off=time(15, 10), delay=2.0)
    assert stop_trigger(bars, DAY, IST, after=at(9, 36), **kw) == at(9, 41, 2)  # bar 09:40 ends 09:41
    assert stop_trigger(bars, DAY, IST, after=at(9, 42), **kw) == at(10, 6, 2)  # earlier bar ignored
    assert stop_trigger(bars, DAY, IST, after=at(10, 7), **kw) is None  # the 15:10 bar ends at the square-off
    assert stop_trigger(bars, DAY, IST, after=at(9, 36), **{**kw, "call": False}) is None  # PE needs > high


def test_run_trade_stop_square_off_end_of_data_and_depth() -> None:
    r = rules()
    sig = at(9, 35, 1)
    quotes = flat_quotes(at(9, 34), at(15, 20))
    t = run_trade(sig, quotes, r, lot_size=65, cost_fn=lambda n, a, b: 40.0, stop_at=lambda e: at(11, 0, 2))
    assert t.status == FILLED and t.exit_reason == "STOP_LOSS" and t.exit_time == at(11, 0, 5)
    assert t.exit_price == 99.7  # min(bid 99.75, ltp 100) - 1 tick
    assert t.net == pytest.approx(t.gross - 40.0) and t.trigger_at == at(11, 0, 2)
    t = run_trade(sig, quotes, r, lot_size=65, cost_fn=lambda n, a, b: 40.0, stop_at=lambda e: None)
    assert t.exit_reason == "SQUARE_OFF" and t.exit_time == at(15, 10)
    t = run_trade(sig, quotes, r, lot_size=65, cost_fn=lambda n, a, b: 40.0, stop_at=lambda e: at(15, 30))
    assert t.exit_reason == "SQUARE_OFF"  # a stop after the square-off never wins
    short = flat_quotes(at(9, 34), at(12, 0))
    t = run_trade(sig, short, r, lot_size=65, cost_fn=lambda n, a, b: 40.0, stop_at=lambda e: None)
    assert t.exit_reason == "END_OF_DATA" and "ended_before_exit_trigger" in t.flags
    thin = [x if x.received < at(11, 0) or x.received >= at(11, 1) else
            Quote(x.received, x.exchange, x.ltp, x.bid, x.ask, 10, 1000) for x in quotes]
    t = run_trade(sig, thin, r, lot_size=65, cost_fn=lambda n, a, b: 40.0, stop_at=lambda e: at(11, 0))
    assert t.exit_time == at(11, 1) and "exit_waited_for_depth" in t.flags


# ---------------------------------------------------------------- variants
CONTRACTS = [Contract(f"K{k}{o}", o, float(k), date(2026, 12, 1), 65)
             for k in range(19500, 20550, 50) for o in ("CE", "PE")]


def inputs(bars: dict, quotes: dict[str, list[Quote]]) -> VariantInputs:
    return VariantInputs(rules=rules(), contracts=CONTRACTS,
                         quotes=lambda k, s, e: [x for x in quotes.get(k, []) if s <= x.received <= e],
                         bars=lambda d: bars, cost_fn=lambda n, a, b: 40.0, tz=IST)


def d2_row(sid: str, when: datetime, key: str | None = "K20000CE", net: float = -500.0) -> dict:
    return {"signal_id": sid, "strategy_id": "orb_options", "option_type": "CE", "generated_at": when.isoformat(),
            "status": FILLED if key else "NO_CONTRACT", "instrument_key": key, "strike": 20000.0 if key else None,
            "expiry": "2026-12-01" if key else None, "lot_size": 65 if key else None, "net": net if key else None}


def test_variant_a_reuses_the_d2_contract_and_uses_the_15_minute_range() -> None:
    closes = {bar_start(m): 20000.0 for m in range(15)}
    closes[time(9, 20)] = 20040.0
    closes[time(9, 25)] = 19960.0
    closes[time(11, 0)] = 19950.0  # below the 19960 low
    bars = bars_from(closes)
    assert orb_range(bars) == (20040.0, 19960.0)
    quotes = {"K20000CE": flat_quotes(at(9, 34), at(15, 20))}
    rows = variant_a([d2_row("S1", at(9, 35, 1)), d2_row("S2", at(9, 40), key=None),
                      {"strategy_id": "vwap_ema", "signal_id": "X"}], inputs(bars, quotes))
    assert [r["status"] for r in rows] == [FILLED, NO_BASE]
    a = rows[0]
    assert a["variant"] == A_NAME and a["exit_reason"] == "STOP_LOSS" and a["trigger_at"] == at(11, 1, 2).isoformat()
    assert a["base_net"] == -500.0 and a["base_status"] == FILLED and a["before_registration"] == 0
    assert set(a) == set(VARIANT_COLUMNS)
    gappy = {k: v for k, v in bars.items() if k != time(9, 20)}
    assert variant_a([d2_row("S1", at(9, 35, 1))], inputs(gappy, quotes))[0]["status"] == NO_RANGE


def retest_bars() -> dict:
    closes = {bar_start(m): 20000.0 for m in range(30)}
    closes[time(9, 20)] = 20040.0  # range 19960-20040 = 0.4 % of 20,000
    closes[time(9, 25)] = 19960.0
    closes[time(9, 50)] = 20060.0  # first break up
    closes[time(10, 0)] = 20010.0  # back inside
    closes[time(10, 20)] = 20070.0  # retest entry (30 min after the first break)
    closes[time(13, 0)] = 19950.0  # stop: close below the low
    return bars_from(closes)


def test_variant_b_finds_the_retest_and_trades_the_atm_option() -> None:
    bars = retest_bars()
    e = retest_signal(DAY, bars)
    assert e is not None and e.direction == 1 and e.spot == 20070.0
    quotes = {"K20050CE": flat_quotes(at(10, 20), at(15, 20))}
    (row,) = variant_b([DAY], inputs(bars, quotes))
    assert row["variant"] == B_NAME and row["signal_id"] == "B30R-2026-12-01" and row["strike"] == 20050.0
    assert row["generated_at"] == at(10, 21, 2).isoformat()  # bar 10:20 ends 10:21, + 2 s
    assert row["status"] == FILLED and row["exit_reason"] == "STOP_LOSS"
    assert row["trigger_at"] == at(13, 1, 2).isoformat()
    assert json.loads(row["info_json"])["range_low"] == 19960.0


def test_variant_b_skips_wide_ranges_and_reports_missing_quotes() -> None:
    bars = retest_bars()
    wide = dict(bars)
    wide[time(9, 21)] = (20200.0, 20200.0, 20200.0, 20200.0)  # range > 0.5 % of the open
    assert retest_signal(DAY, wide) is None and variant_b([DAY], inputs(wide, {})) == []
    (row,) = variant_b([DAY], inputs(bars, {}))
    assert row["status"] == NO_QUOTE


# ---------------------------------------------------------------- report and evaluation
def test_report_pairs_a_with_the_current_orb_and_never_counts_before_registration() -> None:
    early = datetime(2026, 11, 20, 9, 35, tzinfo=IST)
    d2 = [d2_row("S1", at(9, 35), net=-500.0), d2_row("S0", early, net=100.0)]
    rows = [{"variant": A_NAME, "signal_id": "S1", "day": DAY.isoformat(), "status": FILLED, "net": 300.0,
             "base_status": FILLED, "base_net": -500.0},
            {"variant": A_NAME, "signal_id": "S0", "day": "2026-11-20", "status": FILLED, "net": 50.0,
             "base_status": FILLED, "base_net": 100.0},
            {"variant": B_NAME, "signal_id": "B", "day": DAY.isoformat(), "status": FILLED, "net": -200.0}]
    rep = report(d2, rows)
    t = rep["since_registration"]
    assert t["orb"] == {"n": 1, "net": -500.0} and t["A"] == {"n": 1, "net": 300.0}
    assert t["A_minus_orb"] == {"n": 1, "net": 800.0} and t["B"] == {"n": 1, "net": -200.0}
    assert [s["before_registration"] for s in rep["sessions"]] == [True, False]
    assert rep["evaluation"]["A"]["status"] == "NOT_YET_EVALUABLE" and rep["evaluation"]["A"]["n"] == 1
    text = "\n".join(render_text(rep))
    assert "2026-11-20 *" in text and "not yet evaluable" in text


def test_evaluation_needs_the_interval_above_zero_and_both_halves_positive() -> None:
    days = [date(2027, 1, 1) + timedelta(days=i) for i in range(40)]
    good = [100.0 + (i % 5) for i in range(40)]
    t = _test(days, good, 30, 60)
    assert t["status"] == "PASS" and t["stage"] == "FIRST_LOOK" and t["ci975"][0] > 0
    halves = [500.0] * 20 + [-50.0] * 20
    assert _test(days, halves, 30, 40)["status"] == "FAIL"
    assert _test(days, halves, 30, 40)["stage"] == "FINAL"
    assert _test(days[:5], good[:5], 30, 60)["status"] == "NOT_YET_EVALUABLE"


# ---------------------------------------------------------------- store
def test_store_round_trip_and_unknown_columns(tmp_path: Path) -> None:
    s = ResearchStore.open(tmp_path / "r.db")
    row = {c: None for c in VARIANT_COLUMNS} | {"variant": A_NAME, "variant_version": "sv.v1", "signal_id": "S1",
                                                "day": "2026-12-01", "generated_at": at(9, 35).isoformat(),
                                                "option_type": "CE", "before_registration": 0, "status": FILLED}
    assert s.upsert_variants([row, row]) == 2
    assert [r["signal_id"] for r in s.variant_rows("sv.v1")] == ["S1"]
    with pytest.raises(ResearchStoreError, match="unknown variant columns"):
        s.upsert_variants([{**row, "bogus": 1}])
    s.close()
