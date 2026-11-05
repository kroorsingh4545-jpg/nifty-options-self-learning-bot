"""Interfaces the engine depends on (dependency injection).

``core`` imports nothing from other project modules, so the engine talks to
strategies, the ML filter, contract selection and the risk/execution layer
only through these protocols. Concrete implementations are wired together in
``runtime/`` (the composition root).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from core.models import Candle, Instrument, Signal, Tick


@runtime_checkable
class StrategyProtocol(Protocol):
    strategy_id: str
    version: str

    @property
    def enabled(self) -> bool: ...

    def wants(self, candle: Candle) -> bool:
        """True if this strategy consumes this candle (instrument + timeframe)."""
        ...

    def on_candle(self, candle: Candle) -> Sequence[Signal]: ...

    def on_tick(self, tick: Tick) -> Sequence[Signal]: ...

    def on_entry_result(self, signal: Signal, approved: bool) -> None: ...

    def reset_day(self, day: date) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FilterDecision:
    """Output of the ML signal filter. It may veto, or shrink size (multiplier
    in [0, 1]); it can never enlarge size, widen stops or bypass risk."""

    allow: bool
    score: float | None = None  # 0..1 ML confidence; None = no model
    size_multiplier: float = 1.0
    reason: str = ""
    regime: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.size_multiplier <= 1.0:
            raise ValueError("size_multiplier must be within [0, 1]")
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be within [0, 1]")


class SignalFilter(Protocol):
    def evaluate(self, signal: Signal) -> FilterDecision: ...


class ChainedFilter:
    """Several filters in order: every one must allow; size multipliers
    multiply (each is in [0, 1], so the chain can only shrink). The first
    filter's score / regime are reported. A filter that raises vetoes."""

    def __init__(self, *filters: SignalFilter) -> None:
        if not filters:
            raise ValueError("a chain needs at least one filter")
        self.filters = filters

    def evaluate(self, signal: Signal) -> FilterDecision:
        score: float | None = None
        regime: str | None = None
        mult = 1.0
        reasons: list[str] = []
        for i, f in enumerate(self.filters):
            try:
                d = f.evaluate(signal)
            except Exception as exc:  # fail closed
                return FilterDecision(allow=False, score=score, size_multiplier=0.0,
                                      reason=f"filter {i} error: {type(exc).__name__}", regime=regime)
            if i == 0 or score is None:
                score = d.score
            regime = regime or d.regime
            reasons.append(d.reason)
            if not d.allow or d.size_multiplier <= 0:
                return FilterDecision(allow=False, score=score, size_multiplier=0.0, reason=" | ".join(reasons),
                                      regime=regime)
            mult *= d.size_multiplier
        return FilterDecision(allow=True, score=score, size_multiplier=min(1.0, max(0.0, mult)),
                              reason=" | ".join(reasons), regime=regime)


class TradeAdviceAction(StrEnum):
    """What a trade manager (the RL agent) may ask for an OPEN trade. There is
    no action for limits, exposure, the kill switch or square-off timing."""

    HOLD = "HOLD"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    MOVE_STOP_TO_BREAKEVEN = "MOVE_STOP_TO_BREAKEVEN"
    TRAIL_STOP = "TRAIL_STOP"
    TAKE_PARTIAL_PROFIT = "TAKE_PARTIAL_PROFIT"
    EXIT_FULL = "EXIT_FULL"


STOP_ADVICE = frozenset({TradeAdviceAction.TIGHTEN_STOP, TradeAdviceAction.MOVE_STOP_TO_BREAKEVEN,
                         TradeAdviceAction.TRAIL_STOP})


@dataclass(frozen=True)
class TradeAdvice:
    """A proposal for one open trade. ``execution`` re-validates it against
    the live trade (stops only tighten and stay below the market; a partial
    exit keeps at least one lot) before acting; anything else is refused."""

    trade_uid: str
    strategy_id: str
    action: TradeAdviceAction
    new_stop: float | None = None
    exit_lots: int = 0
    reason: str = ""
    confidence: float | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        a = TradeAdviceAction(self.action)
        if a in STOP_ADVICE:
            if self.new_stop is None or not self.new_stop > 0:
                raise ValueError(f"{a.value} needs a positive new_stop")
        elif self.new_stop is not None:
            raise ValueError(f"{a.value} cannot carry a stop")
        if a is TradeAdviceAction.TAKE_PARTIAL_PROFIT:
            if self.exit_lots < 1:
                raise ValueError("TAKE_PARTIAL_PROFIT needs exit_lots >= 1")
        elif self.exit_lots != 0:
            raise ValueError(f"{a.value} cannot carry exit_lots")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


class PassThroughFilter:
    """Default until Phase 7: allows every signal at full (risk-sized) size."""

    def evaluate(self, signal: Signal) -> FilterDecision:
        return FilterDecision(allow=True, score=None, size_multiplier=1.0, reason="no ML model (pass-through)")


@dataclass(frozen=True)
class ContractChoice:
    instrument: Instrument | None
    reasons: tuple[str, ...] = ()


class ContractResolver(Protocol):
    def resolve(self, signal: Signal) -> ContractChoice: ...


@dataclass(frozen=True)
class EntryResult:
    approved: bool
    reasons: tuple[str, ...] = ()
    trade_uid: str | None = None
    quantity: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class EntryGateway(Protocol):
    """Risk check + order routing for an entry (implemented by runtime over
    risk.RiskManager and execution.PositionManager)."""

    async def submit_entry(self, signal: Signal, instrument: Instrument, decision: FilterDecision) -> EntryResult: ...
