"""How closely the simulator reproduces what the bot really did (pure).

Two checks, per signal:

* **against the real trade** (approved signals): same contract and exit reason,
  entry within ``entry_tolerance_ticks``, the real exit within
  ``exit_tolerance_ticks`` of the simulated sampling band (``simulate``), and
  the net P&L difference per lot. Judged on a REPLAY from the ask the risk
  manager logged when there is one, so a 5-second-old decision quote does not
  count against the trade rules; the point-in-time differences are reported
  too (they measure the sampling error itself);
* **against the risk manager's log line** (any signal with one): same contract,
  the ask the stop was set from, and the risk per unit, including the size-zero
  refusals where no trade exists.

A simulator that cannot reproduce the real trades within tolerance must not be
trusted for counterfactuals (D2 exit criterion).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from research.shadow.signals import ShadowSignal
from research.shadow.simulate import Contract, SimResult

#: an approved log signal is matched to the trade created this soon after it
TRADE_MATCH_WINDOW = timedelta(seconds=60)


@dataclass(frozen=True)
class ActualTrade:
    trade_uid: str
    strategy_id: str
    instrument_key: str
    quantity: int
    created_at: datetime
    entry_time: datetime | None
    entry_price: float | None
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str | None
    initial_stop: float
    target: float | None
    gross: float | None
    charges: float

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> ActualTrade:
        def dt(v: Any) -> datetime | None:
            return None if v is None else datetime.fromisoformat(str(v))

        created = dt(r["created_at"])
        assert created is not None
        return cls(str(r["trade_uid"]), str(r["strategy_id"]), str(r["instrument_key"]), int(r["quantity"]),
                   created, dt(r.get("entry_time")), r.get("entry_price"), dt(r.get("exit_time")),
                   r.get("exit_price"), r.get("exit_reason"), float(r["initial_stop_loss"]), r.get("target"),
                   r.get("gross_pnl"), float(r.get("charges") or 0.0))


def match_trade(sig: ShadowSignal, trades: Sequence[ActualTrade]) -> ActualTrade | None:
    if sig.trade_uid is not None:
        return next((t for t in trades if t.trade_uid == sig.trade_uid), None)
    if not sig.approved:
        return None
    near = [t for t in trades if t.strategy_id == sig.strategy_id
            and timedelta(0) <= t.created_at - sig.generated_at <= TRADE_MATCH_WINDOW]
    return min(near, key=lambda t: t.created_at) if near else None


def _ticks(a: float | None, b: float | None, tick: float) -> float | None:
    return None if a is None or b is None else round((a - b) / tick, 1)


def _trade_diffs(sim: SimResult, actual: ActualTrade, tick: float) -> dict[str, Any]:
    lots = actual.quantity / sim.quantity if sim.quantity else 1.0
    actual_net_per_lot = ((actual.gross or 0.0) - actual.charges) / lots
    lo, hi = sim.exit_price, sim.exit_price_best if sim.exit_price_best is not None else sim.exit_price
    return {
        "entry_ticks": _ticks(sim.entry_price, actual.entry_price, tick),
        "exit_ticks": _ticks(sim.exit_price, actual.exit_price, tick),
        "exit_band": [lo, hi],
        "exit_outside_band_ticks": (None if lo is None or hi is None or actual.exit_price is None else round(
            max(0.0, min(lo, hi) - actual.exit_price, actual.exit_price - max(lo, hi)) / tick, 1)),
        "exit_reason_match": sim.exit_reason == actual.exit_reason,
        "simulated_exit_reason": sim.exit_reason,
        "net_per_lot_diff": None if sim.net is None else round(sim.net - actual_net_per_lot, 2),
        "stop_ticks": _ticks(sim.stop, actual.initial_stop, tick),
    }


def compare(sig: ShadowSignal, contract: Contract | None, sim: SimResult, actual: ActualTrade | None, *,
            tick: float, entry_tol: float, exit_tol: float, replay: SimResult | None = None) -> dict[str, Any] | None:
    """The calibration record of one signal, or None when there is nothing to compare.

    ``sim`` is the point-in-time counterfactual (what is stored); ``replay`` the
    same signal replayed from the ask the risk manager logged. A real trade is
    REPRODUCED when the contract and exit reason match, the entry is within
    ``entry_tol`` ticks and the real exit lies within ``exit_tol`` ticks of the
    simulated sampling band, judged on the replay when there is one (it removes
    the decision-quote sampling error) and on the point-in-time run otherwise."""
    out: dict[str, Any] = {"signal_id": sig.signal_id, "strategy_id": sig.strategy_id}
    lg = sig.logged
    if lg is not None:
        if lg.instrument_key is not None:
            out["logged_contract_match"] = contract is not None and contract.instrument_key == lg.instrument_key
        out["logged_entry_ref_ticks"] = _ticks(sim.entry_ref, lg.entry_ref, tick)
        out["logged_risk_per_unit_diff"] = (None if sim.risk_per_unit is None or lg.risk_per_unit is None
                                            else round(sim.risk_per_unit - lg.risk_per_unit, 2))
    if actual is not None:
        out["trade_uid"] = actual.trade_uid
        out["contract_match"] = contract is not None and contract.instrument_key == actual.instrument_key
        out["actual_entry"], out["actual_exit"] = actual.entry_price, actual.exit_price
        out["actual_exit_reason"] = actual.exit_reason
        out["simulated"] = sim.status
        if actual.entry_price is None or actual.exit_price is None:
            out["within_tolerance"] = False
            return out
        if sim.status == "FILLED":
            out["point_in_time"] = _trade_diffs(sim, actual, tick)
        if replay is not None and replay.status == "FILLED":
            out["replay"] = _trade_diffs(replay, actual, tick)
        judged = out.get("replay") or out.get("point_in_time")
        out["judged_on"] = "replay" if "replay" in out else ("point_in_time" if judged else None)
        out["within_tolerance"] = bool(
            judged and out["contract_match"] and judged["exit_reason_match"]
            and abs(judged["entry_ticks"] or 0) <= entry_tol
            and (judged["exit_outside_band_ticks"] or 0) <= exit_tol)
    return out if len(out) > 2 else None


__all__ = ["TRADE_MATCH_WINDOW", "ActualTrade", "compare", "match_trade"]
