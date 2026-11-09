"""Trading engine: ticks -> candles -> strategy signals -> ML filter ->
contract selection -> risk + execution.

::

    WebSocket tick ─▶ CandleBuilder ─▶ engine.on_candle ─▶ strategy.on_candle ─▶ Signal
                                                                                  │
      SignalFilter.evaluate (ML-ready; veto / shrink only) ◀──────────────────────┘
                 │ allow
      ContractResolver.resolve (strike selection, liquidity)
                 │ instrument
      EntryGateway.submit_entry (RiskManager gate -> PositionManager -> OrderManager)

* Runs only while RUNNING and inside market hours.
* Signals are processed one at a time (single entry lock) so open-trade caps
  cannot be raced by concurrent signals.
* Fail-closed: if the filter or resolver raises, the entry is refused.
* A strategy that raises is disabled for the rest of the day; other strategies
  are unaffected (isolation).
* PAUSED / STOPPED only stop NEW entries. Open positions keep their stops,
  targets, trailing, square-off and kill-switch protection (execution layer).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from core.clock import now_ist
from core.event_bus import EventBus, Topic
from core.interfaces import (
    ContractResolver,
    EntryGateway,
    EntryResult,
    FilterDecision,
    PassThroughFilter,
    SignalFilter,
    StrategyProtocol,
)
from core.models import Candle, Signal, Tick
from core.state_manager import EngineState, StateManager
from core.tasks import wait_for_tasks

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        *,
        strategies: Sequence[StrategyProtocol],
        resolver: ContractResolver,
        gateway: EntryGateway,
        signal_filter: SignalFilter | None = None,
        bus: EventBus | None = None,
        state: StateManager | None = None,
        market_hours: Callable[[datetime], bool],
        now_fn: Callable[[], datetime] = now_ist,
    ) -> None:
        ids = [s.strategy_id for s in strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate strategy ids")
        self.strategies = {s.strategy_id: s for s in strategies}
        self.resolver = resolver
        self.gateway = gateway
        self.filter: SignalFilter = signal_filter or PassThroughFilter()
        self.bus = bus or EventBus()
        self.state = state or StateManager(ids)
        self._market_hours = market_hours
        self._now = now_fn
        self._entry_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def _set_state(self, st: EngineState) -> None:
        old = self.state.engine_state
        self.state.engine_state = st
        if old is not st:
            log.info("engine state", extra={"from": old.value, "to": st.value})
            self.bus.publish(Topic.ENGINE_STATE, {"from": old.value, "to": st.value})

    def start(self) -> None:
        self._set_state(EngineState.RUNNING)

    def pause(self) -> None:
        self._set_state(EngineState.PAUSED)

    def stop(self) -> None:
        self._set_state(EngineState.STOPPED)

    @property
    def accepting_signals(self) -> bool:
        return self.state.engine_state is EngineState.RUNNING and self._market_hours(self._now())

    def new_day(self, day: date) -> None:
        self.state.reset_day(day)
        for s in self.strategies.values():
            try:
                s.reset_day(day)
            except Exception:
                log.exception("strategy reset_day failed", extra={"strategy_id": s.strategy_id})
        self.bus.publish(Topic.DAY_RESET, {"day": day.isoformat()})

    # ------------------------------------------------------------------ #
    # Data in
    # ------------------------------------------------------------------ #
    def _usable(self, s: StrategyProtocol) -> bool:
        return s.enabled and self.state.strategy(s.strategy_id).errored is None

    def _strategy_failed(self, s: StrategyProtocol, where: str) -> None:
        rt = self.state.strategy(s.strategy_id)
        rt.errored = f"exception in {where}"
        log.exception("strategy raised; disabled for the day", extra={"strategy_id": s.strategy_id})
        self.bus.publish(Topic.STRATEGY_ERROR, {"strategy_id": s.strategy_id, "where": where})

    def on_candle(self, candle: Candle) -> None:
        self.bus.publish(Topic.CANDLE, candle)
        if not self.accepting_signals:
            self._observe(candle)
            return
        for s in self.strategies.values():
            if not self._usable(s) or not s.wants(candle):
                continue
            try:
                signals = list(s.on_candle(candle))
            except Exception:
                self._strategy_failed(s, "on_candle")
                continue
            for sig in signals:
                self._spawn(self.process_signal(s, sig))

    def _observe(self, candle: Candle) -> None:
        """Not accepting signals (paused / stopped): indicators stay current, so
        resuming does not trade on a gap. No signal can come out of this."""
        for s in self.strategies.values():
            observe = getattr(s, "observe_candle", None)
            if observe is None or not self._usable(s) or not s.wants(candle):
                continue
            try:
                observe(candle)
            except Exception:
                self._strategy_failed(s, "observe_candle")

    def on_tick(self, tick: Tick) -> None:
        if not self.accepting_signals:
            return
        for s in self.strategies.values():
            if not self._usable(s):
                continue
            try:
                signals = list(s.on_tick(tick))
            except Exception:
                self._strategy_failed(s, "on_tick")
                continue
            for sig in signals:
                self._spawn(self.process_signal(s, sig))

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        await wait_for_tasks(self._tasks)
        await self.bus.drain()

    # ------------------------------------------------------------------ #
    # Signal pipeline
    # ------------------------------------------------------------------ #
    async def process_signal(self, strategy: StrategyProtocol, signal: Signal) -> EntryResult:
        if signal.strategy_id != strategy.strategy_id:
            raise ValueError("strategy emitted a signal for another strategy")
        rt = self.state.strategy(strategy.strategy_id)
        rt.signals += 1
        rt.last_signal = signal
        self.bus.publish(Topic.SIGNAL, signal)
        now = self._now()
        async with self._entry_lock:
            if not self.accepting_signals:
                return self._finish(strategy, signal, EntryResult(False, ("engine not accepting signals",)), None)
            try:
                decision = self.filter.evaluate(signal)
            except Exception:
                log.exception("signal filter failed; refusing entry (fail-closed)",
                              extra={"strategy_id": strategy.strategy_id})
                decision = FilterDecision(allow=False, reason="signal filter error")
            rt.last_ml_score, rt.last_regime = decision.score, decision.regime
            if not decision.allow or decision.size_multiplier <= 0:
                rt.filtered_out += 1
                self.bus.publish(Topic.SIGNAL_FILTERED, {"signal": signal, "reason": decision.reason})
                return self._finish(strategy, signal, EntryResult(False, (f"filtered: {decision.reason}",)),
                                    decision)
            try:
                choice = self.resolver.resolve(signal)
            except Exception:
                log.exception("contract resolution failed", extra={"strategy_id": strategy.strategy_id})
                return self._finish(strategy, signal, EntryResult(False, ("contract resolution error",)), decision)
            if choice.instrument is None:
                return self._finish(strategy, signal, EntryResult(False, choice.reasons or ("no contract",)),
                                    decision)
            try:
                result = await self.gateway.submit_entry(signal, choice.instrument, decision)
            except Exception as exc:
                log.exception("entry submission failed", extra={"strategy_id": strategy.strategy_id})
                result = EntryResult(False, (f"entry error: {type(exc).__name__}: {exc}",))
        rt.record("entry", {"approved": result.approved, "reasons": list(result.reasons)}, now)
        return self._finish(strategy, signal, result, decision)

    def _finish(self, s: StrategyProtocol, sig: Signal, result: EntryResult,
                decision: FilterDecision | None) -> EntryResult:
        rt = self.state.strategy(s.strategy_id)
        if result.approved:
            rt.approved_entries += 1
        else:
            rt.rejected_entries += 1
        rt.last_decision = "APPROVED" if result.approved else "; ".join(result.reasons)[:300]
        try:
            s.on_entry_result(sig, result.approved)
        except Exception:
            self._strategy_failed(s, "on_entry_result")
        self.bus.publish(Topic.ENTRY_DECISION, {"signal_id": sig.signal_id, "strategy_id": s.strategy_id,
                                                "approved": result.approved, "reasons": list(result.reasons),
                                                "trade_uid": result.trade_uid, "quantity": result.quantity,
                                                "ml_score": decision.score if decision else None})
        log.info("signal processed", extra={"strategy_id": s.strategy_id, "signal_id": sig.signal_id,
                                            "approved": result.approved, "reasons": list(result.reasons)})
        return result
