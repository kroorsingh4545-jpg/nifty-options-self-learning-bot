"""Base class for strategies.

A strategy turns completed candles into ``Signal`` objects and nothing else:
* It never imports or calls ``broker/`` or ``execution/`` (architecture test).
* It owns only its own state; capital, sizing, stops and limits are applied
  downstream by ``risk/`` using the strategy's config.
* Entry-window checks here are a convenience — ``RiskManager`` enforces the
  window and the 14:45 cutoff again, authoritatively.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from config.settings import StrategyConfig
from core.clock import now_ist
from core.enums import OptionType
from core.models import Candle, Signal, Tick


class StrategyParamsError(ValueError):
    pass


class BaseStrategy(ABC):
    params_model: type[BaseModel]

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfig,
        *,
        signal_instrument_key: str,
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        self._now = now_fn  # injected clock: simulated time in backtests, IST wall clock live
        self.strategy_id = strategy_id
        self.config = config
        self.version = config.version
        self.signal_instrument_key = signal_instrument_key
        try:
            self.params: Any = self.params_model.model_validate(config.params)
        except Exception as exc:
            raise StrategyParamsError(f"{strategy_id}: invalid params: {exc}") from exc
        self.day: date | None = None
        self.entries: dict[OptionType, int] = {OptionType.CE: 0, OptionType.PE: 0}
        self.signals_today = 0
        self.reset_day(self._now().date())

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def timeframe(self) -> int:
        return self.config.timeframe_minutes

    def wants(self, candle: Candle) -> bool:
        return candle.instrument_key == self.signal_instrument_key and candle.complete and \
            candle.timeframe_minutes in self.timeframes_used()

    def timeframes_used(self) -> set[int]:
        return {self.timeframe}

    def in_entry_window(self, t: datetime) -> bool:
        w = self.config.entry_window
        return w.start <= t.time() < w.end

    def reset_day(self, day: date) -> None:
        self.day = day
        self.entries = {OptionType.CE: 0, OptionType.PE: 0}
        self.signals_today = 0
        self._reset_state(day)

    def _roll(self, candle: Candle) -> None:
        d = candle.start.date()
        if d != self.day:
            self.reset_day(d)

    def on_candle(self, candle: Candle) -> Sequence[Signal]:
        self._roll(candle)
        return self.generate(candle)

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return ()

    def on_entry_result(self, signal: Signal, approved: bool) -> None:
        if approved:
            self.entries[signal.option_type] += 1

    def make_signal(self, opt: OptionType, candle: Candle, reason: str, features: dict[str, float]) -> Signal:
        self.signals_today += 1
        return Signal(
            signal_id=f"SIG-{self.strategy_id}-{uuid.uuid4().hex[:12]}",
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            option_type=opt,
            generated_at=self._now(),
            candle_end=candle.end,
            underlying_key=candle.instrument_key,
            underlying_price=candle.close,
            reason=reason,
            features={k: round(float(v), 6) for k, v in features.items()},
        )

    def snapshot(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "version": self.version, "enabled": self.enabled,
                "day": self.day.isoformat() if self.day else None, "signals_today": self.signals_today,
                "entries": {k.value: v for k, v in self.entries.items()}, **self._snapshot()}

    # ------------------------------------------------------------------ #
    @abstractmethod
    def _reset_state(self, day: date) -> None: ...

    @abstractmethod
    def generate(self, candle: Candle) -> Sequence[Signal]: ...

    def _snapshot(self) -> dict[str, Any]:
        return {}
