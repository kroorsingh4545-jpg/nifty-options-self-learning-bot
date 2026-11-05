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
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config.settings import BotConfig
from core.clock import ensure_ist, now_ist
from core.enums import ModelType, TradingMode
from core.interfaces import FilterDecision
from core.models import Signal
from ml.feature_engineering import FeatureEngine, LookaheadError, OptionContext, PastOutcome, feature_vector
from ml.feature_store import FeatureStore
from ml.model_registry import LoadedModel, ModelRegistry, SyntheticModelError
from ml.regime_classifier import RuleBasedRegime
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


__all__ = ["InferenceConfigError", "InferenceService", "StrategyModels", "load_strategy_models"]
