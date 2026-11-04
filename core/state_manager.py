"""Engine and per-strategy runtime state.

Each strategy has its own ``StrategyRuntime`` record — nothing is shared, so a
failure in one strategy (e.g. an exception in its signal code) disables only
that strategy for the day.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from core.models import Signal


class EngineState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # no new entries; open positions remain protected


@dataclass
class StrategyRuntime:
    strategy_id: str
    day: date | None = None
    signals: int = 0
    filtered_out: int = 0
    approved_entries: int = 0
    rejected_entries: int = 0
    last_signal: Signal | None = None
    last_decision: str | None = None
    last_ml_score: float | None = None
    last_regime: str | None = None
    errored: str | None = None  # set when the strategy raised; disabled for the day
    recent: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, detail: dict[str, Any], at: datetime) -> None:
        self.recent.append({"kind": kind, "at": at.isoformat(), **detail})
        del self.recent[:-50]


class StateManager:
    def __init__(self, strategy_ids: Iterable[str]) -> None:
        self.engine_state = EngineState.STOPPED
        self._strategies = {sid: StrategyRuntime(sid) for sid in strategy_ids}

    def strategy(self, sid: str) -> StrategyRuntime:
        return self._strategies[sid]

    def strategy_ids(self) -> list[str]:
        return list(self._strategies)

    def reset_day(self, day: date) -> None:
        for sid in self._strategies:
            self._strategies[sid] = StrategyRuntime(sid, day=day)

    def snapshot(self) -> dict[str, Any]:
        return {
            "engine_state": self.engine_state.value,
            "strategies": {
                sid: {
                    "day": s.day.isoformat() if s.day else None,
                    "signals": s.signals,
                    "filtered_out": s.filtered_out,
                    "approved_entries": s.approved_entries,
                    "rejected_entries": s.rejected_entries,
                    "last_signal": s.last_signal.model_dump(mode="json") if s.last_signal else None,
                    "last_decision": s.last_decision,
                    "ml_score": s.last_ml_score,
                    "regime": s.last_regime,
                    "errored": s.errored,
                }
                for sid, s in self._strategies.items()
            },
        }
