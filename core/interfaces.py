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
