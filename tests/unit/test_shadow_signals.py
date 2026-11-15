"""D2: signals from the bot's ``signals`` table and rebuilt from its JSON log
(SYNTHETIC records in the bot's real log format)."""

from __future__ import annotations

import json
from typing import Any

from research.shadow.signals import from_log_records, from_table_rows, merge, parse_log_lines


def rec(ts: str, event: str, sid: str = "vwap_ema", **kw: Any) -> dict[str, Any]:
    return {"ts": f"2026-11-12T{ts}+05:30", "level": "INFO", "event": event, "mode": "PAPER", "strategy_id": sid,
            **kw}


LOG = [
    rec("11:19:59.638", "strategy candle evaluated", "orb_options", candle_end="2026-11-12T11:20:00+05:30",
        signal=False, reason="opening range invalid", close=25295.75),
    rec("11:20:00.092", "strategy candle evaluated", candle_end="2026-11-12T11:20:00+05:30", signal=True,
        reason="VWAP+EMA momentum: 2 closes below VWAP", close=25328.7, bull=False, bear=True),
    rec("11:20:00.576", "entry rejected by risk",
        reasons=["position size zero: risk budget 2000.00 too small for one lot at 39.30/unit"]),
    rec("11:20:00.593", "signal processed", signal_id="SIG-A", approved=False,
        reasons=["position size zero: risk budget 2000.00 too small for one lot at 39.30/unit"]),
    rec("13:50:00.100", "strategy candle evaluated", "orb_options", candle_end="2026-11-12T13:50:00+05:30",
        signal=True, reason="ORB breakout: close 25400.00 > high", close=25400.0),
    rec("13:50:00.234", "entry approved by risk", "orb_options", instrument="NSE_FO|45391", qty=65, entry=138.25,
        stop=110.6, target=193.55),
    rec("13:50:00.300", "signal processed", "orb_options", signal_id="SIG-B", approved=True, reasons=[]),
    rec("14:00:00.300", "signal processed", "orb_options", signal_id="SIG-NO-CANDLE", approved=False,
        reasons=["engine not accepting signals"]),
]


def test_signals_are_rebuilt_from_the_three_log_lines() -> None:
    signals, logged = from_log_records(LOG)
    by_id = {s.signal_id: s for s in signals}
    assert set(by_id) == {"SIG-A", "SIG-B"}  # SIG-NO-CANDLE has no decision line: not rebuilt
    a, b = by_id["SIG-A"], by_id["SIG-B"]
    assert (a.option_type, a.approved, a.underlying_price, a.source) == ("PE", False, 25328.7, "LOG")
    assert a.candle_end.isoformat() == "2026-11-12T11:20:00+05:30" and a.trade_uid is None
    assert (b.option_type, b.approved) == ("CE", True)
    assert logged["SIG-A"].risk_per_unit == 39.3 and logged["SIG-A"].entry_ref is None
    lb = logged["SIG-B"]
    assert (lb.instrument_key, lb.entry_ref, lb.stop, lb.risk_per_unit) == ("NSE_FO|45391", 138.25, 110.6, 27.65)


def test_a_decision_line_too_far_before_is_not_used() -> None:
    old = [rec("11:00:00.000", "strategy candle evaluated", candle_end="2026-11-12T11:00:00+05:30", signal=True,
               reason="x", close=1.0, bull=True, bear=False),
           rec("11:20:00.593", "signal processed", signal_id="SIG-LATE", approved=False, reasons=[])]
    assert from_log_records(old)[0] == []


def test_strategies_never_borrow_each_others_lines() -> None:
    mixed = [rec("10:00:00.000", "strategy candle evaluated", "orb_options", candle_end="2026-11-12T10:00:00+05:30",
                 signal=True, reason="ORB breakdown: close", close=24000.0),
             rec("10:00:00.100", "signal processed", "vwap_ema", signal_id="SIG-X", approved=False, reasons=[])]
    assert from_log_records(mixed)[0] == []


def test_table_signals_win_and_are_enriched_from_the_log() -> None:
    table = from_table_rows([{
        "signal_id": "SIG-B", "strategy_id": "orb_options", "option_type": "CE",
        "generated_at": "2026-11-12T13:50:00.100+05:30", "candle_end": "2026-11-12T13:50:00+05:30",
        "underlying_price": 25400.0, "decided": 1, "approved": 1, "decision_reasons_json": "[]",
        "trade_uid": "PAPER-T-orb_options-1"}])
    log, logged = from_log_records(LOG)
    merged = merge(table, log, logged)
    assert [s.signal_id for s in merged] == ["SIG-A", "SIG-B"]
    b = merged[1]
    assert b.source == "TABLE" and b.trade_uid == "PAPER-T-orb_options-1"
    assert b.logged is not None and b.logged.entry_ref == 138.25


def test_log_lines_that_are_not_json_objects_are_skipped() -> None:
    lines = [json.dumps(LOG[0]), "not json", "", "[1, 2]", "{broken", json.dumps(LOG[1])]
    assert len(parse_log_lines(lines)) == 2
