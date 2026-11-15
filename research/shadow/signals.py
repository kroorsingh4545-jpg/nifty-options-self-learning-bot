"""The strategy signals shadow evaluation runs on.

Two sources, both read-only and passed in as plain rows by the composition
root:

* the bot's ``signals`` table (trading database v7 onwards): every signal
  with its entry decision;
* the bot's JSON log (``logs/bot.jsonl``) for sessions before v7. A signal is
  rebuilt from three lines the bot writes for every signal: ``strategy candle
  evaluated`` with ``signal: true`` (direction, candle end, price),
  ``entry approved / rejected by risk`` (the contract, the ask the stop was set
  from, the stop, or the risk per unit of a size-zero refusal), and
  ``signal processed`` (signal id and outcome). The log has no trade id: an
  approved log signal is matched to its trade by time (``calibrate``).

The same log lines also enrich table signals with what the risk manager saw
(``LoggedDecision``), so the simulator's contract and entry quote can be
checked against the bot's own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

#: how far before ``signal processed`` its candle line and risk line may be
EVAL_WINDOW = timedelta(seconds=10)
RISK_WINDOW = timedelta(seconds=5)
_SIZE_ZERO = re.compile(r"one lot at ([0-9]+(?:\.[0-9]+)?)/unit")


@dataclass(frozen=True)
class LoggedDecision:
    """What the risk manager logged for a signal."""

    approved: bool
    instrument_key: str | None = None
    entry_ref: float | None = None  # the ask the stop was computed from
    stop: float | None = None
    risk_per_unit: float | None = None  # entry - stop, or from a size-zero refusal


@dataclass(frozen=True)
class ShadowSignal:
    signal_id: str
    strategy_id: str
    option_type: str  # CE | PE
    generated_at: datetime
    candle_end: datetime
    underlying_price: float
    decided: bool
    approved: bool | None
    reasons: tuple[str, ...]
    trade_uid: str | None
    source: str  # TABLE | LOG
    logged: LoggedDecision | None = None


def _dt(v: Any) -> datetime:
    return datetime.fromisoformat(str(v))


def from_table_rows(rows: Iterable[Mapping[str, Any]]) -> list[ShadowSignal]:
    """Rows of the bot's ``signals`` table."""
    out = []
    for r in rows:
        approved = r.get("approved")
        out.append(ShadowSignal(
            signal_id=str(r["signal_id"]), strategy_id=str(r["strategy_id"]), option_type=str(r["option_type"]),
            generated_at=_dt(r["generated_at"]), candle_end=_dt(r["candle_end"]),
            underlying_price=float(r["underlying_price"]), decided=bool(r["decided"]),
            approved=None if approved is None else bool(approved),
            reasons=tuple(json.loads(r.get("decision_reasons_json") or "[]")), trade_uid=r.get("trade_uid"),
            source="TABLE"))
    return out


def _direction(ev: Mapping[str, Any]) -> str | None:
    if ev.get("bull") is True:
        return "CE"
    if ev.get("bear") is True:
        return "PE"
    reason = str(ev.get("reason") or "")
    if reason.startswith("ORB breakout"):
        return "CE"
    if reason.startswith("ORB breakdown"):
        return "PE"
    return None


def _logged(risk: Mapping[str, Any]) -> LoggedDecision:
    if risk.get("event") == "entry approved by risk":
        entry, stop = risk.get("entry"), risk.get("stop")
        rpu = round(float(entry) - float(stop), 4) if entry is not None and stop is not None else None
        return LoggedDecision(True, risk.get("instrument"), None if entry is None else float(entry),
                              None if stop is None else float(stop), rpu)
    rpu = None
    for reason in risk.get("reasons") or ():
        m = _SIZE_ZERO.search(str(reason))
        if m:
            rpu = float(m.group(1))
    return LoggedDecision(False, risk_per_unit=rpu)


def from_log_records(records: Iterable[Mapping[str, Any]]) -> tuple[list[ShadowSignal], dict[str, LoggedDecision]]:
    """Rebuild signals from the bot's JSON log records (in file order).
    Returns (signals, {signal_id: what the risk manager logged})."""
    last_eval: dict[str, Mapping[str, Any]] = {}
    last_risk: dict[str, Mapping[str, Any]] = {}
    signals: list[ShadowSignal] = []
    logged: dict[str, LoggedDecision] = {}
    for rec in records:
        event, sid = rec.get("event"), rec.get("strategy_id")
        if not sid or "ts" not in rec:
            continue
        if event == "strategy candle evaluated" and rec.get("signal") is True:
            last_eval[sid] = rec
        elif event in ("entry approved by risk", "entry rejected by risk"):
            last_risk[sid] = rec
        elif event == "signal processed" and rec.get("signal_id"):
            at = _dt(rec["ts"])
            risk = last_risk.pop(sid, None)
            if risk is not None and timedelta(0) <= at - _dt(risk["ts"]) <= RISK_WINDOW:
                logged[str(rec["signal_id"])] = _logged(risk)
            ev = last_eval.pop(sid, None)
            if ev is None or not timedelta(0) <= at - _dt(ev["ts"]) <= EVAL_WINDOW:
                continue
            direction = _direction(ev)
            if direction is None or ev.get("candle_end") is None or ev.get("close") is None:
                continue
            signals.append(ShadowSignal(
                signal_id=str(rec["signal_id"]), strategy_id=str(sid), option_type=direction,
                generated_at=_dt(ev["ts"]), candle_end=_dt(ev["candle_end"]), underlying_price=float(ev["close"]),
                decided=True, approved=bool(rec.get("approved")),
                reasons=tuple(str(x) for x in rec.get("reasons") or ()), trade_uid=None, source="LOG"))
    return signals, logged


def parse_log_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """JSON log lines -> records; a line that is not a JSON object is skipped."""
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def merge(table: Iterable[ShadowSignal], log: Iterable[ShadowSignal],
          logged: Mapping[str, LoggedDecision]) -> list[ShadowSignal]:
    """Table signals first (they are complete); log signals the table does not
    have; every signal enriched with what the risk manager logged for it."""
    by_id: dict[str, ShadowSignal] = {s.signal_id: s for s in log}
    by_id.update({s.signal_id: s for s in table})
    return sorted((replace(s, logged=logged.get(s.signal_id)) for s in by_id.values()),
                  key=lambda s: (s.generated_at, s.signal_id))


__all__ = ["EVAL_WINDOW", "RISK_WINDOW", "LoggedDecision", "ShadowSignal", "from_log_records", "from_table_rows",
           "merge", "parse_log_lines"]
