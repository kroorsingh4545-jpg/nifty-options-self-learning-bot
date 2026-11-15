"""Shadow evaluation of recorded signals (D2): one counterfactual outcome per
signal, plus calibration against the real PAPER trades.

The composition root wires the data access in (``ShadowInputs``): recorded
quotes from the research archive, the NIFTY index price, the contract list,
the bot's closed trades (read-only), the per-strategy rules from the bot
configuration and the bot's own charge calculator. Nothing here can place an
order or change a strategy, a risk limit or a configuration value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from research.shadow.calibrate import ActualTrade, compare, match_trade
from research.shadow.rules import TradeRules
from research.shadow.signals import ShadowSignal
from research.shadow.simulate import NO_CONTRACT, NO_SPOT, Contract, Quote, SimResult, select_contract, simulate

SESSION_CLOSE = time(15, 30)


@dataclass(frozen=True)
class ShadowInputs:
    rules: Mapping[str, TradeRules]  # strategy_id -> rules (bot configuration values)
    contracts: Sequence[Contract]
    quotes: Callable[[str, datetime, datetime], list[Quote]]  # instrument, from, to (receipt time)
    spot: Callable[[datetime], tuple[float, str] | None]  # NIFTY index at a time -> (price, source)
    cost_fn: Callable[[int, float, float], float]  # quantity, entry, exit -> round-trip charges
    trades: Sequence[ActualTrade] = ()
    sim_version: str = "d2.1"
    entry_tolerance_ticks: float = 1.0
    exit_tolerance_ticks: float = 2.0


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


def outcome_row(sig: ShadowSignal, spot: tuple[float, str] | None, contract: Contract | None, sim: SimResult,
                sim_version: str) -> dict[str, Any]:
    return {
        "signal_id": sig.signal_id, "sim_version": sim_version, "strategy_id": sig.strategy_id,
        "option_type": sig.option_type, "generated_at": sig.generated_at.isoformat(),
        "candle_end": sig.candle_end.isoformat(), "signal_source": sig.source, "decided": int(sig.decided),
        "approved": None if sig.approved is None else int(sig.approved),
        "decision_reasons_json": json.dumps(list(sig.reasons)), "trade_uid": sig.trade_uid,
        "spot": None if spot is None else spot[0], "spot_source": None if spot is None else spot[1],
        "instrument_key": None if contract is None else contract.instrument_key,
        "strike": None if contract is None else contract.strike,
        "expiry": None if contract is None else contract.expiry.isoformat(),
        "lot_size": None if contract is None else contract.lot_size,
        "status": sim.status, "decision_quote_at": _iso(sim.decision_quote_at), "entry_ref": sim.entry_ref,
        "stop": sim.stop, "target": sim.target, "risk_per_unit": sim.risk_per_unit,
        "entry_time": _iso(sim.entry_time), "entry_price": sim.entry_price, "exit_time": _iso(sim.exit_time),
        "exit_price": sim.exit_price, "exit_price_best": sim.exit_price_best, "exit_reason": sim.exit_reason,
        "final_stop": sim.final_stop,
        "quantity": sim.quantity, "gross": sim.gross, "charges": sim.charges, "net": sim.net,
        "r_gross": sim.r_gross, "r_net": sim.r_net, "mfe_r": sim.mfe_r, "mae_r": sim.mae_r,
        "quotes_used": sim.quotes_used, "max_gap_seconds": sim.max_gap_seconds,
        "liquidity_json": json.dumps(sim.liquidity), "flags_json": json.dumps(sim.flags),
    }


def evaluate(signals: Iterable[ShadowSignal], inp: ShadowInputs) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(outcome rows, calibration records) for ``signals``."""
    rows: list[dict[str, Any]] = []
    calib: list[dict[str, Any]] = []
    for sig in signals:
        r = inp.rules.get(sig.strategy_id)
        contract: Contract | None = None
        replay: SimResult | None = None
        spot = inp.spot(sig.generated_at)
        if r is None:
            sim = SimResult(NO_CONTRACT, flags=["no rules for this strategy in the bot configuration"])
        elif spot is None:
            sim = SimResult(NO_SPOT)
        else:
            day = sig.generated_at.date()
            contract = select_contract(spot[0], sig.option_type, day, inp.contracts, mode=r.strike_mode,
                                       offset=r.strike_offset, step=r.strike_step)
            if contract is None:
                sim = SimResult(NO_CONTRACT)
            else:
                start = sig.generated_at - timedelta(seconds=r.max_quote_age_seconds + 1)
                end = datetime.combine(day, SESSION_CLOSE, tzinfo=sig.generated_at.tzinfo)
                recorded = inp.quotes(contract.instrument_key, start, end)
                sim = simulate(sig.generated_at, recorded, r, lot_size=contract.lot_size, cost_fn=inp.cost_fn)
                lg = sig.logged
                if lg is not None and lg.entry_ref is not None and lg.instrument_key == contract.instrument_key:
                    replay = simulate(sig.generated_at, recorded, r, lot_size=contract.lot_size,
                                      cost_fn=inp.cost_fn, decision_ask=lg.entry_ref)
        rows.append(outcome_row(sig, spot, contract, sim, inp.sim_version))
        if r is not None:
            c = compare(sig, contract, sim, match_trade(sig, inp.trades), tick=r.tick_size,
                        entry_tol=inp.entry_tolerance_ticks, exit_tol=inp.exit_tolerance_ticks, replay=replay)
            if c is not None:
                calib.append(c)
    return rows, calib


__all__ = ["SESSION_CLOSE", "ShadowInputs", "evaluate", "outcome_row"]
