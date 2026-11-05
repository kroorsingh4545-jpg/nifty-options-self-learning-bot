"""Inference service: the ML signal filter the trading engine calls.

``InferenceService`` implements ``core.interfaces.SignalFilter``. For every
strategy signal it

1. passes through untouched (score None, full size) when the strategy's ML
   is disabled (``strategies.<id>.ml.enabled: false``, the default);
2. otherwise computes the schema-v1 features AS OF the decision time with the
   same ``compute_features`` used in training (live ``FeatureEngine`` fed by
   the bot's completed 1-minute candles; the option context is the contract
   the strategy's strike rules select, from the live quote);
3. scores it with the strategy's OWN signal-filter model (per-strategy model
   state: a model registered for another strategy is refused), applies the
   strategy's ``FilterPolicy`` (ALLOW / REDUCE / DELAY / REJECT), and adds the
   regime label (regime model, else the trailing rules) and the outcome
   predictions for the audit trail;
4. records a SIGNAL feature snapshot (features, score, decision, model
   versions) in ``feature_snapshots``.

Fail-closed: ML enabled but no model, stale feature data, a lookahead error,
a model error, or a missing / invalid score -> the entry is REFUSED. The
decision only ever vetoes or shrinks (``FilterDecision`` enforces a size
multiplier in [0, 1]); stops, targets and every risk limit stay with
``risk/``, which re-checks the entry after this filter.

Model policy: production use accepts only HISTORICAL models in a deployable
status (``ModelRegistry.load_production``). SYNTHETIC / research models are
accepted only with ``research=True``, which is refused outside PAPER mode
and meant for backtests on research data.

Phase 8 adds ``RLTradeAdvisor``: the PPO trade manager. It vetoes entries
(ENTER / SKIP_SETUP) and proposes HOLD / stop tightening / partial profit /
full exit for open trades, under the same model policy. Its proposals are
re-validated by ``execution.position_manager.PositionManager.apply_advice``.

Phase 9: every RL decision is persisted to ``rl_decisions``. Entry decisions
are written here (``RLTradeAdvisor.evaluate``); an ENTER whose record cannot
be written is turned into a veto (an unaudited approval is never acted on).
Trade-management decisions are written by ``runtime.rl`` together with what
execution did with them; a strategy whose decision cannot be written is
suspended from RL for the session (``RLTradeAdvisor.suspend``): its entries
are refused and its open trades keep their deterministic stops.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config.settings import BotConfig
from core.clock import IST, ensure_ist, now_ist
from core.enums import ModelType, RLDecisionKind, TradingMode
from core.ids import new_uid
from core.interfaces import FilterDecision, TradeAdvice, TradeAdviceAction
from core.models import Signal
from data.storage.records import RLDecisionRecord
from data.storage.repositories import RLDecisionRepository
from ml.feature_engineering import FeatureEngine, LookaheadError, OptionContext, PastOutcome, feature_vector
from ml.feature_store import FeatureStore
from ml.model_registry import RL_KIND, LoadedModel, ModelRegistry, SyntheticModelError
from ml.regime_classifier import RuleBasedRegime
from ml.rl_environment import (
    STOP_ACTIONS,
    AccountView,
    Action,
    PositionView,
    RiskEnvelope,
    SetupView,
    action_mask,
    build_observation,
    observation_vector,
    partial_lots,
    proposed_stop,
)
from ml.signal_filter import FilterPolicy

log = logging.getLogger(__name__)

EXPECTED_KIND = {"signal_filter": "signal_filter", "outcome": "outcome_predictor", "regime": "regime_classifier"}


class InferenceConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategyModels:
    """One strategy's model state (never shared between strategies)."""

    strategy_id: str
    signal_filter: LoadedModel | None = None
    outcome: LoadedModel | None = None
    regime: LoadedModel | None = None

    def loaded(self) -> dict[str, LoadedModel]:
        return {k: m for k in EXPECTED_KIND if (m := getattr(self, k)) is not None}

    def versions(self) -> dict[str, str]:
        return {k: m.record.model_version for k, m in self.loaded().items()}


def load_strategy_models(registry: ModelRegistry, cfg: BotConfig,
                         mode: TradingMode = TradingMode.PAPER) -> dict[str, StrategyModels]:
    """Production models for every ML-enabled strategy (HISTORICAL only)."""
    out: dict[str, StrategyModels] = {}
    for sid, scfg in cfg.strategies.strategies.items():
        if not scfg.ml.enabled:
            continue
        out[sid] = StrategyModels(sid, registry.load_production(ModelType.SIGNAL_FILTER, sid, mode),
                                  registry.load_production(ModelType.OUTCOME_PREDICTOR, sid, mode),
                                  registry.load_production(ModelType.REGIME_CLASSIFIER, sid, mode))
    return out


OptionContextFn = Callable[[Signal, datetime], OptionContext | None]
OutcomesFn = Callable[[str], Sequence[PastOutcome]]


class InferenceService:
    def __init__(self, cfg: BotConfig, *, features: FeatureEngine, option_context: OptionContextFn,
                 outcomes: OutcomesFn, models: Mapping[str, StrategyModels], mode: TradingMode = TradingMode.PAPER,
                 research: bool = False, store: FeatureStore | None = None,
                 max_bar_age: timedelta = timedelta(minutes=2), now_fn: Callable[[], datetime] = now_ist) -> None:
        if research and TradingMode(mode) is not TradingMode.PAPER:
            raise InferenceConfigError("research (SYNTHETIC) models are only allowed in PAPER mode")
        for sid, sm in models.items():
            if sm.strategy_id != sid:
                raise InferenceConfigError(f"models keyed {sid!r} belong to {sm.strategy_id!r}")
            for slot, m in sm.loaded().items():
                if m.kind != EXPECTED_KIND[slot]:
                    raise InferenceConfigError(f"{sid}.{slot}: expected a {EXPECTED_KIND[slot]}, got {m.kind}")
                if m.record.strategy_id != sid:
                    raise InferenceConfigError(f"{sid}.{slot}: model {m.record.model_version} was trained for "
                                               f"{m.record.strategy_id!r} (strategy model state is isolated)")
                if m.is_synthetic and not research:
                    raise SyntheticModelError(f"{sid}.{slot}: {m.record.model_version} was trained on "
                                              f"{m.record.data_source.value} data and cannot be used for trading")
        self.cfg = cfg
        self.features = features
        self._option_context = option_context
        self._outcomes = outcomes
        self.models = dict(models)
        self.mode = TradingMode(mode)
        self.research = research
        self.store = store
        self.max_bar_age = max_bar_age
        self._now = now_fn
        self.last: dict[str, dict[str, Any]] = {}  # strategy -> last decision (dashboard / audit)

    # ------------------------------------------------------------------ #
    def evaluate(self, signal: Signal) -> FilterDecision:
        sid = signal.strategy_id
        scfg = self.cfg.strategies.strategies.get(sid)
        if scfg is None:
            return self._refuse(signal, "unknown strategy")
        if not scfg.ml.enabled:
            return FilterDecision(allow=True, score=None, size_multiplier=1.0, reason="ML disabled (pass-through)",
                                  regime=self._rules_regime(signal))
        sm = self.models.get(sid)
        if sm is None or sm.signal_filter is None:
            return self._refuse(signal, "ML enabled but no deployable signal-filter model (fail-closed)")
        as_of = ensure_ist(self._now())
        if as_of < signal.candle_end:
            return self._refuse(signal, "signal is stamped after the decision time")
        last = self.features.last_bar_end
        if last is None or as_of - last > self.max_bar_age:
            return self._refuse(signal, f"feature data stale (last NIFTY bar {last.isoformat() if last else None})")
        try:
            option = self._option_context(signal, as_of)
            feats = self.features.features(as_of, direction=signal.option_type, option=option,
                                           outcomes=self._outcomes(sid))
            x = feature_vector(feats).reshape(1, -1)  # one row, schema order
            score = float(sm.signal_filter.model.score(x)[0])
        except LookaheadError as exc:
            return self._refuse(signal, f"lookahead guard: {exc}")
        except Exception as exc:
            log.exception("ML inference failed; entry refused (fail-closed)", extra={"strategy_id": sid})
            return self._refuse(signal, f"ML inference error: {type(exc).__name__}")
        verdict = FilterPolicy.from_config(scfg.ml).decide(score if math.isfinite(score) else None)
        regime_label, regime, outcome = self._extras(sm, x, feats)
        meta = {"signal_id": signal.signal_id, "score": verdict.score, "action": verdict.action.value,
                "size_multiplier": verdict.size_multiplier, "reason": verdict.reason, "regime": regime,
                "outcome": outcome, "models": sm.versions(), "research": self.research,
                "candle_end": signal.candle_end.isoformat(), "decided_at": as_of.isoformat()}
        self.last[sid] = meta
        self._snapshot(signal, as_of, feats, meta)
        tag = f" [{sm.signal_filter.record.model_version}]"
        return FilterDecision(allow=verdict.allow, score=verdict.score, size_multiplier=verdict.size_multiplier,
                              reason=f"{verdict.action.value}: {verdict.reason}{tag}", regime=regime_label)

    # ------------------------------------------------------------------ #
    def _extras(self, sm: StrategyModels, x: Any, feats: dict[str, float]) -> tuple[str, dict[str, Any],
                                                                                     dict[str, float] | None]:
        """Regime and outcome predictions (informational; failures never block)."""
        try:
            reg = sm.regime.model.predict(x)[0] if sm.regime is not None else RuleBasedRegime.from_features(feats)
        except Exception:
            log.exception("regime prediction failed; using trailing rules")
            reg = RuleBasedRegime.from_features(feats)
        outcome = None
        if sm.outcome is not None:
            try:
                outcome = sm.outcome.model.predict(x)[0].as_dict()
            except Exception:
                log.exception("outcome prediction failed")
        return reg.label, reg.as_dict(), outcome

    def _rules_regime(self, signal: Signal) -> str | None:
        try:
            as_of = max(ensure_ist(self._now()), signal.candle_end)
            return RuleBasedRegime.from_features(self.features.features(as_of)).label
        except Exception:
            return None

    def _refuse(self, signal: Signal, reason: str) -> FilterDecision:
        self.last[signal.strategy_id] = {"signal_id": signal.signal_id, "action": "REJECT", "reason": reason,
                                         "score": None}
        log.warning("ML filter refused entry", extra={"strategy_id": signal.strategy_id, "reason": reason})
        return FilterDecision(allow=False, score=None, size_multiplier=0.0, reason=reason)

    def _snapshot(self, signal: Signal, as_of: datetime, feats: dict[str, float], meta: dict[str, Any]) -> None:
        if self.store is None:
            return
        try:
            self.store.record_signal(strategy_id=signal.strategy_id, instrument_key=signal.underlying_key,
                                     as_of=as_of, features=feats, extra=meta)
        except Exception:
            log.exception("could not record the ML feature snapshot", extra={"strategy_id": signal.strategy_id})


# --------------------------------------------------------------------------- #
# RL trade manager (Phase 8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeContext:
    """One OPEN trade as the runtime sees it (built from the position manager)."""

    strategy_id: str
    trade_uid: str
    position: PositionView
    option: OptionContext | None  # the trade's contract, with its latest quote (stamped <= as-of)
    premium: float | None  # latest traded price of the contract
    tick_size: float
    account: AccountView


AccountFn = Callable[[str], AccountView]


def load_rl_models(registry: ModelRegistry, cfg: BotConfig, mode: TradingMode = TradingMode.PAPER
                   ) -> dict[str, LoadedModel]:
    """Production RL models (HISTORICAL, deployable) for every RL-enabled strategy."""
    out: dict[str, LoadedModel] = {}
    for sid, scfg in cfg.strategies.strategies.items():
        if scfg.rl.enabled:
            m = registry.load_production(ModelType.RL_PPO, sid, mode)
            if m is not None:
                out[sid] = m
    return out


class RLTradeAdvisor:
    """The RL agent as a trade manager.

    * ``evaluate(signal)`` (a ``SignalFilter``): ENTER approves the entry at
      the size risk computes, SKIP_SETUP vetoes it. The agent can veto, never
      create or enlarge an entry. Strategies without RL pass through.
      RL enabled but no model / stale data / any error -> REFUSED.
    * ``advise(ctx)``: HOLD / tighten the stop / partial profit / full exit
      for an open trade, as a ``TradeAdvice`` that ``execution`` re-validates.
      Any problem -> HOLD, which leaves the trade exactly as the deterministic
      stop, target, trailing and square-off rules manage it.

    It never sees or touches daily loss limits, exposure caps, the kill
    switch, credentials or square-off timing. SYNTHETIC (research) models are
    accepted only with ``research=True``, which is refused outside PAPER.
    """

    def __init__(self, cfg: BotConfig, *, features: FeatureEngine, models: Mapping[str, LoadedModel],
                 option_context: OptionContextFn, account: AccountFn, outcomes: OutcomesFn,
                 mode: TradingMode = TradingMode.PAPER, research: bool = False,
                 max_bar_age: timedelta = timedelta(minutes=2), now_fn: Callable[[], datetime] = now_ist,
                 decisions: RLDecisionRepository | None = None) -> None:
        if research and TradingMode(mode) is not TradingMode.PAPER:
            raise InferenceConfigError("research (SYNTHETIC) RL models are only allowed in PAPER mode")
        if decisions is not None and decisions.mode is not TradingMode(mode):
            raise InferenceConfigError(f"RL decisions repository is bound to {decisions.mode.value}, the advisor "
                                       f"runs in {TradingMode(mode).value}")
        for sid, m in models.items():
            if m.kind != RL_KIND or m.record.model_type is not ModelType.RL_PPO:
                raise InferenceConfigError(f"{sid}: expected an {RL_KIND} model, got {m.kind}")
            if m.record.strategy_id != sid:
                raise InferenceConfigError(f"{sid}: RL model {m.record.model_version} was trained for "
                                           f"{m.record.strategy_id!r} (strategy model state is isolated)")
            if m.is_synthetic and not research:
                raise SyntheticModelError(f"{sid}: {m.record.model_version} was trained on "
                                          f"{m.record.data_source.value} data and cannot be used for trading")
        self.cfg = cfg
        self.features = features
        self.models = dict(models)
        self._option_context = option_context
        self._account = account
        self._outcomes = outcomes
        self.mode = TradingMode(mode)
        self.research = research
        self.max_bar_age = max_bar_age
        self._now = now_fn
        self.envelopes = {sid: RiskEnvelope.from_config(cfg, sid) for sid in self.models}
        self.last: dict[str, dict[str, Any]] = {}  # strategy -> last decision (dashboard / audit)
        self._last_advice: dict[str, datetime] = {}  # trade -> last decision time
        self.decisions = decisions  # every decision is persisted here (None: not persisted, e.g. offline tools)
        self.suspended: dict[str, str] = {}  # strategy -> why RL is off for the session (decision log failure)
        self.record_failures = 0

    # Decision log ----------------------------------------------------------
    def suspend(self, strategy_id: str, reason: str) -> None:
        """Turn RL off for this strategy for the rest of the session: its
        entries are refused and its trades keep their deterministic stops."""
        if strategy_id not in self.suspended:
            self.suspended[strategy_id] = reason
            log.critical("RL suspended for the session: entries refused, trades keep their configured stops",
                         extra={"strategy_id": strategy_id, "reason": reason})

    def record_decision(self, **fields: Any) -> bool:
        """Build AND persist one decision inside one guard (``decision_record``
        fields). Any failure - an invalid record or a failed write - returns
        False and suspends RL for the strategy; it never raises into the
        caller (the candle listener / the entry filter)."""
        sid = str(fields.get("strategy_id", "?"))
        try:
            rec = self.decision_record(**fields)
        except Exception as exc:
            self.record_failures += 1
            log.exception("could not build the RL decision record", extra={"strategy_id": sid})
            self.suspend(sid, f"RL decision record invalid: {type(exc).__name__}")
            return False
        return self.record(rec)

    def record(self, rec: RLDecisionRecord) -> bool:
        """Persist one decision. False (and the strategy suspended) on failure."""
        if self.decisions is None:
            return True
        try:
            self.decisions.insert(rec)
            return True
        except Exception as exc:
            self.record_failures += 1
            log.exception("could not persist the RL decision", extra={"strategy_id": rec.strategy_id})
            self.suspend(rec.strategy_id, f"RL decision log write failed: {type(exc).__name__}")
            return False

    def decision_record(self, *, kind: RLDecisionKind, strategy_id: str, action: str, decided_at: datetime,
                        reason: str, trade_uid: str | None = None, signal_id: str | None = None,
                        allowed: bool | None = None, applied: bool | None = None, result: str | None = None,
                        new_stop: float | None = None, exit_lots: int | None = None,
                        confidence: float | None = None, model_version: str | None = None,
                        details: Mapping[str, Any] | None = None) -> RLDecisionRecord:
        conf = confidence if confidence is not None and math.isfinite(confidence) and 0 <= confidence <= 1 else None
        clean = {k: (v if not isinstance(v, float) or math.isfinite(v) else None)
                 for k, v in (details or {}).items()}
        if confidence is not None and conf is None:
            clean["raw_confidence"] = repr(confidence)
        return RLDecisionRecord(
            decision_uid=new_uid("RLD"), mode=self.mode, strategy_id=strategy_id, kind=kind, trade_uid=trade_uid,
            signal_id=signal_id, action=action, allowed=allowed, applied=applied, result=result,
            new_stop=new_stop, exit_lots=exit_lots, confidence=conf, model_version=model_version,
            research=self.research, reason=reason[:500], details_json=clean, decided_at=ensure_ist(decided_at),
            created_at=ensure_ist(decided_at))

    def enabled(self, strategy_id: str) -> bool:
        s = self.cfg.strategies.strategies.get(strategy_id)
        return s is not None and s.rl.enabled

    def due(self, trade_uid: str, strategy_id: str, as_of: datetime) -> bool:
        """True when the strategy's decision interval has elapsed for this trade."""
        last = self._last_advice.get(trade_uid)
        interval = timedelta(minutes=self.cfg.strategies.strategies[strategy_id].rl.decision_interval_minutes)
        return last is None or as_of - last >= interval - timedelta(seconds=1)

    def _observation(self, sid: str, as_of: datetime, *, option: OptionContext | None, premium: float | None,
                     position: PositionView | None, setup: SetupView | None, account: AccountView
                     ) -> dict[str, float]:
        market = self.features.features(as_of)
        return build_observation(
            market=market, spot=self.features.last_close(as_of),
            session_open_price=self.features.session_open(as_of.astimezone(IST).date()), as_of=as_of,
            times=self.features.times, no_new_entries_after=self.cfg.risk.time_rules.no_new_entries_after,
            option=option, premium=premium, position=position, setup=setup, account=account,
            outcomes=list(self._outcomes(sid))[-20:], regime=RuleBasedRegime.from_features(market))

    def _stale(self, as_of: datetime) -> str | None:
        last = self.features.last_bar_end
        if last is None or as_of - last > self.max_bar_age:
            return f"feature data stale (last NIFTY bar {last.isoformat() if last else None})"
        return None

    # Entry approval ------------------------------------------------------
    def evaluate(self, signal: Signal) -> FilterDecision:
        sid = signal.strategy_id
        if not self.enabled(sid):
            return FilterDecision(allow=True, score=None, size_multiplier=1.0, reason="RL disabled (pass-through)")
        if sid in self.suspended:
            decision = self._veto(sid, signal.signal_id, f"RL suspended: {self.suspended[sid]}")
        else:
            decision = self._evaluate(signal)
        info = dict(self.last.get(sid, {}))
        action = str(info.pop("action", "VETO"))
        recorded = self.record_decision(
            kind=RLDecisionKind.ENTRY, strategy_id=sid, signal_id=signal.signal_id, action=action,
            decided_at=self._now(), allowed=decision.allow, reason=decision.reason,
            confidence=_float(info.get("confidence")), model_version=_str(info.pop("model", None)),
            details={k: v for k, v in info.items() if k != "signal_id"})
        if not recorded and decision.allow:
            return self._veto(sid, signal.signal_id, "RL decision could not be recorded (fail-closed)")
        return decision

    def _evaluate(self, signal: Signal) -> FilterDecision:
        sid = signal.strategy_id
        m = self.models.get(sid)
        if m is None:
            return self._veto(sid, signal.signal_id, "RL enabled but no deployable RL model (fail-closed)")
        as_of = ensure_ist(self._now())
        stale = self._stale(as_of)
        if stale or as_of < signal.candle_end:
            return self._veto(sid, signal.signal_id, stale or "signal is stamped after the decision time")
        try:
            option = self._option_context(signal, as_of)
            raw = self._observation(sid, as_of, option=option, premium=option.premium if option else None,
                                    position=None, setup=SetupView(signal.option_type, None),
                                    account=self._account(sid))
            action, info = m.model.act(observation_vector(raw), action_mask(False, True))
        except LookaheadError as exc:
            return self._veto(sid, signal.signal_id, f"lookahead guard: {exc}")
        except Exception as exc:
            log.exception("RL entry decision failed; entry refused (fail-closed)", extra={"strategy_id": sid})
            return self._veto(sid, signal.signal_id, f"RL inference error: {type(exc).__name__}")
        tag = f"[{m.record.model_version}]"
        self.last[sid] = {"signal_id": signal.signal_id, "action": action.name, "decided_at": as_of.isoformat(),
                          "model": m.record.model_version, "research": self.research, **info}
        if action is Action.ENTER:
            return FilterDecision(allow=True, score=None, size_multiplier=1.0, reason=f"RL ENTER {tag}")
        return FilterDecision(allow=False, score=None, size_multiplier=0.0, reason=f"RL {action.name} {tag}")

    def _veto(self, sid: str, signal_id: str, reason: str) -> FilterDecision:
        self.last[sid] = {"signal_id": signal_id, "action": "VETO", "reason": reason}
        log.warning("RL refused entry", extra={"strategy_id": sid, "reason": reason})
        return FilterDecision(allow=False, score=None, size_multiplier=0.0, reason=reason)

    # Trade management ----------------------------------------------------
    def advise(self, ctx: TradeContext) -> TradeAdvice:
        sid = ctx.strategy_id
        as_of = ensure_ist(self._now())

        def hold(reason: str) -> TradeAdvice:
            self.last[sid] = {"trade": ctx.trade_uid, "action": "HOLD", "reason": reason,
                              "decided_at": as_of.isoformat()}
            return TradeAdvice(ctx.trade_uid, sid, TradeAdviceAction.HOLD, reason=reason)

        if not self.enabled(sid):
            return hold("RL disabled")
        if sid in self.suspended:
            return hold(f"RL suspended: {self.suspended[sid]}")
        m = self.models.get(sid)
        if m is None:
            return hold("no RL model: the trade keeps its configured stop / target / trailing")
        if as_of.time() >= self.cfg.risk.time_rules.square_off_start:
            return hold("square-off window")
        stale = self._stale(as_of)
        if stale:
            return hold(stale)
        self._last_advice[ctx.trade_uid] = as_of
        try:
            raw = self._observation(sid, as_of, option=ctx.option, premium=ctx.premium, position=ctx.position,
                                    setup=None, account=ctx.account)
            action, info = m.model.act(observation_vector(raw), action_mask(True, False))
        except Exception as exc:
            log.exception("RL advice failed; holding (deterministic stops stay in force)", extra={"strategy_id": sid})
            return hold(f"RL inference error: {type(exc).__name__}")
        rules = self.cfg.strategies.strategies[sid].rl
        price = ctx.premium or 0.0
        tag, conf = m.record.model_version, float(info.get("confidence", 0.0))
        self.last[sid] = {"trade": ctx.trade_uid, "action": action.name, "decided_at": as_of.isoformat(),
                          "model": tag, "research": self.research, **info}
        if action in STOP_ACTIONS:
            new = proposed_stop(action, ctx.position, price, rules, ctx.tick_size)
            if new is None:
                return hold(f"{action.name} would not tighten")
            return TradeAdvice(ctx.trade_uid, sid, TradeAdviceAction(action.name), new_stop=new,
                               reason=f"RL {action.name}", confidence=conf, model_version=tag)
        if action is Action.TAKE_PARTIAL_PROFIT:
            lots = partial_lots(ctx.position, price, rules)
            if lots is None:
                return hold("partial profit not allowed now")
            return TradeAdvice(ctx.trade_uid, sid, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=lots,
                               reason="RL TAKE_PARTIAL_PROFIT", confidence=conf, model_version=tag)
        if action is Action.EXIT_FULL:
            return TradeAdvice(ctx.trade_uid, sid, TradeAdviceAction.EXIT_FULL, reason="RL EXIT_FULL",
                               confidence=conf, model_version=tag)
        return TradeAdvice(ctx.trade_uid, sid, TradeAdviceAction.HOLD, reason="RL HOLD", confidence=conf,
                           model_version=tag)

    def forget(self, trade_uid: str) -> None:
        self._last_advice.pop(trade_uid, None)


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    return None if v is None else str(v)


__all__ = ["InferenceConfigError", "InferenceService", "RLTradeAdvisor", "StrategyModels", "TradeContext",
           "load_rl_models", "load_strategy_models"]
