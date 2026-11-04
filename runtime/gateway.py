"""EntryGateway over the risk + execution stack."""

from __future__ import annotations

from broker.broker_interface import Broker
from config.settings import BotConfig
from core.interfaces import EntryResult, FilterDecision
from core.models import Instrument, Signal
from execution.order_manager import EntryBlockedError
from execution.stack import ExecutionStack
from risk.risk_manager import EntryProposal


class RiskEntryGateway:
    """Signal + contract + ML decision -> RiskManager -> PositionManager."""

    def __init__(self, cfg: BotConfig, stack: ExecutionStack, broker: Broker) -> None:
        self.cfg = cfg
        self.stack = stack
        self.broker = broker

    async def submit_entry(self, signal: Signal, instrument: Instrument, decision: FilterDecision) -> EntryResult:
        if instrument.option_type is not signal.option_type:
            return EntryResult(False, ("contract option type does not match signal",))
        proposal = EntryProposal(
            strategy_id=signal.strategy_id, instrument=instrument, option_type=signal.option_type,
            reason=signal.reason, size_multiplier=decision.size_multiplier, ml_confidence=decision.score,
            regime=decision.regime, underlying_price=signal.underlying_price,
        )
        funds = await self.broker.get_funds()
        risk = self.stack.risk.evaluate_entry(proposal, available_margin=funds.available_margin)
        if not risk.approved:
            return EntryResult(False, risk.reasons)
        try:
            trade = await self.stack.positions.open_trade(risk, strategy_version=signal.strategy_version)
        except EntryBlockedError as exc:
            return EntryResult(False, (str(exc),))
        details = {"entry_ref": risk.entry_price, "risk_amount": risk.sizing.risk_amount if risk.sizing else None,
                   "stop": risk.levels.stop if risk.levels else None,
                   "target": risk.levels.target if risk.levels else None,
                   "instrument": instrument.trading_symbol}
        return EntryResult(True, (), trade.trade_uid, risk.quantity, details)
