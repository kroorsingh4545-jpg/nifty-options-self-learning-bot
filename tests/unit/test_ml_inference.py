"""Phase 7 part 5: inference service and its wiring into the bot / backtester.

Models here are either SYNTHETIC research models or fixed-score stubs (not
trained models): they exist only to exercise the plumbing. Stubs are marked
SYNTHETIC so the same rules apply to them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from backtest.backtester import Backtester, BacktestOptions, with_ml_enabled
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import BotConfig
from core.clock import IST
from core.enums import ModelDataSource, ModelType, OptionType, TradingMode
from core.interfaces import FilterDecision
from core.models import Signal
from data.storage.records import ModelRunRecord
from data.storage.repositories import Repositories
from ml.feature_engineering import FEATURE_NAMES, FeatureEngine, OptionContext, SessionTimes, compute_features
from ml.feature_store import FeatureStore
from ml.inference_service import InferenceConfigError, InferenceService, StrategyModels, load_strategy_models
from ml.model_registry import LoadedModel, ModelRegistry, SyntheticModelError
from ml.signal_filter import FilterPolicy, SignalFilterModel
from tests.ml_support import config
from tests.unit.test_ml_features import INDEX, synthetic_day

D = date(2026, 11, 4)
TIMES = SessionTimes()


class StubFilter:
    """A fixed-score stand-in for a signal-filter model (NOT a trained model)."""

    def __init__(self, score: float, *, fail: bool = False) -> None:
        self.value, self.fail, self.calls = score, fail, 0

    def score(self, X: np.ndarray) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise RuntimeError("model exploded")
        assert X.shape == (1, len(FEATURE_NAMES))
        return np.full(1, self.value)


def stub(strategy_id: str, model: Any, *, kind: str = "signal_filter",
         source: ModelDataSource = ModelDataSource.SYNTHETIC) -> LoadedModel:
    mt = {"signal_filter": ModelType.SIGNAL_FILTER, "regime_classifier": ModelType.REGIME_CLASSIFIER,
          "outcome_predictor": ModelType.OUTCOME_PREDICTOR}[kind]
    rec = ModelRunRecord(run_id=f"RUN-{strategy_id}-{kind}", model_type=mt, model_version=f"stub-{kind}-synthetic",
                         strategy_id=strategy_id, feature_schema_version="1.0.0", feature_schema_json={},
                         training_start=datetime(2026, 1, 1, tzinfo=IST), training_end=datetime(2026, 2, 1, tzinfo=IST),
                         artifact_path="-", data_source=source)
    return LoadedModel(rec, {"model_kind": kind, "synthetic": source is not ModelDataSource.HISTORICAL}, model,
                       Path("-"))


def ml_cfg(*sids: str) -> BotConfig:
    return with_ml_enabled(config(), sids) if sids else config()


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def make_service(cfg: BotConfig, models: dict[str, StrategyModels], *, research: bool = True,
                 option: OptionContext | None = None, store: FeatureStore | None = None,
                 feed_until: datetime | None = None) -> tuple[InferenceService, Clock]:
    clock = Clock(datetime(2026, 11, 4, 10, 30, tzinfo=IST))
    eng = FeatureEngine(index_key=INDEX, times=TIMES)
    until = feed_until or clock.now
    for c in synthetic_day(D):
        if c.end <= until:
            eng.on_candle(c)
    svc = InferenceService(cfg, features=eng, option_context=lambda s, t: option, outcomes=lambda sid: (),
                           models=models, research=research, store=store, now_fn=clock)
    return svc, clock


def signal(sid: str = "orb_options", at: datetime | None = None, opt: OptionType = OptionType.CE) -> Signal:
    t = at or datetime(2026, 11, 4, 10, 30, tzinfo=IST)
    return Signal(signal_id=f"SIG-{sid}-x", strategy_id=sid, strategy_version="0.1.0", option_type=opt,
                  generated_at=t, candle_end=t, underlying_key=INDEX, underlying_price=25_000.0, reason="test")


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


def test_ml_disabled_strategy_passes_through_untouched() -> None:
    svc, _ = make_service(ml_cfg(), {})
    d = svc.evaluate(signal())
    assert d.allow and d.score is None and d.size_multiplier == 1.0 and "pass-through" in d.reason
    assert d.regime is not None  # trailing-rule regime label for the dashboard


def test_ml_enabled_without_a_model_fails_closed() -> None:
    svc, _ = make_service(ml_cfg("orb_options"), {})
    d = svc.evaluate(signal())
    assert not d.allow and d.size_multiplier == 0.0 and "no deployable" in d.reason
    other = svc.evaluate(signal("vwap_ema"))  # strategy isolation: the other strategy is unaffected
    assert other.allow and other.score is None


@pytest.mark.parametrize(("score", "allow", "mult"), [(0.9, True, 1.0), (0.65, True, 0.75), (0.57, False, 0.0),
                                                     (0.2, False, 0.0)])
def test_decision_follows_the_strategy_policy(score: float, allow: bool, mult: float, paper: Repositories) -> None:
    cfg = ml_cfg("orb_options")
    model = StubFilter(score)
    svc, clock = make_service(cfg, {"orb_options": StrategyModels("orb_options", stub("orb_options", model))},
                              store=FeatureStore(paper.feature_snapshots))
    d = svc.evaluate(signal())
    want = FilterPolicy.from_config(cfg.strategies.strategies["orb_options"].ml).decide(score)
    assert (d.allow, d.size_multiplier, d.score) == (allow, pytest.approx(mult), want.score)
    assert d.reason.startswith(want.action.value) and "stub-signal_filter-synthetic" in d.reason
    assert d.regime and "|" in d.regime
    row = paper.db.query_one("SELECT * FROM feature_snapshots WHERE strategy_id = 'orb_options'")
    assert row is not None and row["purpose"] == "SIGNAL"
    assert svc.last["orb_options"]["action"] == want.action.value


def test_features_at_inference_equal_the_training_features() -> None:
    cfg = ml_cfg("orb_options")
    seen: list[np.ndarray] = []

    class Spy(StubFilter):
        def score(self, X: np.ndarray) -> np.ndarray:
            seen.append(X.copy())
            return np.full(1, 0.9)

    opt = OptionContext(OptionType.CE, 25_000.0, D + timedelta(days=6), datetime(2026, 11, 4, 10, 29, tzinfo=IST),
                        premium=120.0, day_volume=5e4, oi=1e6, bid=119.9, ask=120.1)
    svc, clock = make_service(cfg, {"orb_options": StrategyModels("orb_options", stub("orb_options", Spy(0)))},
                              option=opt)
    svc.evaluate(signal())
    from ml.feature_engineering import Bars

    want = compute_features(Bars.from_candles(synthetic_day(D)), clock.now, times=TIMES, direction=OptionType.CE,
                            option=opt)
    np.testing.assert_array_equal(np.isnan(seen[0][0]), np.isnan([want[k] for k in FEATURE_NAMES]))
    np.testing.assert_allclose(np.nan_to_num(seen[0][0]), np.nan_to_num([want[k] for k in FEATURE_NAMES]))


def test_fail_closed_paths() -> None:
    cfg = ml_cfg("orb_options")
    models = {"orb_options": StrategyModels("orb_options", stub("orb_options", StubFilter(0.9, fail=True)))}
    svc, _ = make_service(cfg, models)
    assert not svc.evaluate(signal()).allow  # model error
    ok = {"orb_options": StrategyModels("orb_options", stub("orb_options", StubFilter(0.9)))}
    stale, _ = make_service(cfg, ok, feed_until=datetime(2026, 11, 4, 10, 0, tzinfo=IST))
    d = stale.evaluate(signal())
    assert not d.allow and "stale" in d.reason
    late = OptionContext(OptionType.CE, 25_000.0, D, datetime(2026, 11, 4, 10, 31, tzinfo=IST), premium=100.0)
    look, _ = make_service(cfg, ok, option=late)
    d = look.evaluate(signal())
    assert not d.allow and "lookahead" in d.reason
    future, _ = make_service(cfg, ok)
    d = future.evaluate(signal(at=datetime(2026, 11, 4, 10, 35, tzinfo=IST)))
    assert not d.allow and "after the decision time" in d.reason
    nan, _ = make_service(cfg, {"orb_options": StrategyModels("orb_options", stub("orb_options",
                                                                                  StubFilter(float("nan"))))})
    assert not nan.evaluate(signal()).allow


def test_filter_never_enlarges_size() -> None:
    cfg = ml_cfg("orb_options")
    for s in np.linspace(0, 1, 41):
        svc, _ = make_service(cfg, {"orb_options": StrategyModels("orb_options", stub("orb_options",
                                                                                      StubFilter(float(s))))})
        d = svc.evaluate(signal())
        assert isinstance(d, FilterDecision) and 0.0 <= d.size_multiplier <= 1.0


# --------------------------------------------------------------------------- #
# Model policy
# --------------------------------------------------------------------------- #


def test_synthetic_models_are_refused_outside_research_mode() -> None:
    models = {"orb_options": StrategyModels("orb_options", stub("orb_options", StubFilter(0.9)))}
    with pytest.raises(SyntheticModelError, match="cannot be used for trading"):
        make_service(ml_cfg("orb_options"), models, research=False)


def test_research_mode_is_paper_only() -> None:
    eng = FeatureEngine(index_key=INDEX, times=TIMES)
    with pytest.raises(InferenceConfigError, match="PAPER"):
        InferenceService(config(), features=eng, option_context=lambda s, t: None, outcomes=lambda s: (), models={},
                         mode=TradingMode.LIVE, research=True)


def test_a_model_of_another_strategy_is_refused() -> None:
    wrong = {"orb_options": StrategyModels("orb_options", stub("vwap_ema", StubFilter(0.9)))}
    with pytest.raises(InferenceConfigError, match="isolated"):
        make_service(ml_cfg("orb_options"), wrong)
    kind = {"orb_options": StrategyModels("orb_options", stub("orb_options", StubFilter(0.9),
                                                              kind="regime_classifier"))}
    with pytest.raises(InferenceConfigError, match="expected a signal_filter"):
        make_service(ml_cfg("orb_options"), kind)


def test_production_loader_never_returns_synthetic_models(paper: Repositories, tmp_path: Path) -> None:
    research = ModelRegistry(paper.model_runs, tmp_path / "m", allow_synthetic=True)
    rng = np.random.default_rng(0)
    X, y = rng.normal(size=(300, len(FEATURE_NAMES))), rng.integers(0, 2, 300)
    m = SignalFilterModel.fit(X, y, num_boost_round=5)
    research.register(m, kind="signal_filter", strategy_id="orb_options", data_source="SYNTHETIC",
                      manifest={"feature_schema": {"version": "1.0.0"},
                                "training_window": {"start": "2026-01-01T09:15:00+05:30",
                                                    "end": "2026-02-01T15:10:00+05:30"},
                                "hyperparameters": {"seeds": [11, 23, 37, 41, 53]}, "environment": {
                                    "python": "x", "lightgbm": "x", "numpy": "x", "pandas": "x", "pipeline": "x",
                                    "feature_schema": "1.0.0"}, "evaluation": {"note": "synthetic"}},
                      report_md="SYNTHETIC")
    models = load_strategy_models(ModelRegistry(paper.model_runs, tmp_path / "m"), ml_cfg("orb_options"))
    assert models["orb_options"].signal_filter is None  # the synthetic run is not deployable


# --------------------------------------------------------------------------- #
# Bot / backtest wiring
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def tiny_dataset():  # type: ignore[no-untyped-def]
    return generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=3, seed=14,
                                                    strike_margin_points=200))


def _bt(cfg: BotConfig, ds: Any, **kw: Any):  # type: ignore[no-untyped-def]
    return asyncio.run(Backtester(cfg, ds, BacktestOptions(**kw)).run())


@pytest.fixture(scope="module")
def baseline(tiny_dataset):  # type: ignore[no-untyped-def]
    return _bt(config(), tiny_dataset)


def test_backtest_with_a_rejecting_filter_takes_no_trades(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    assert baseline.closed_trades(), "the SYNTHETIC baseline must trade for this test to mean anything"
    sids = tuple(baseline.strategies)
    models = {s: StrategyModels(s, stub(s, StubFilter(0.1))) for s in sids}
    res = _bt(config(), tiny_dataset, ml_strategies=sids, ml_models=models, ml_research=True)
    assert res.trades == []
    assert sum(res.observations.signals.values()) > 0  # signals were generated, then filtered out
    reasons = {r for c in res.observations.rejections.values() for r in c}
    assert any(r.startswith("filtered: REJECT") for r in reasons)


def test_backtest_with_a_reducing_filter_shrinks_size_and_keeps_risk_controls(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    sids = tuple(baseline.strategies)
    models = {s: StrategyModels(s, stub(s, StubFilter(0.62))) for s in sids}  # REDUCE: x0.6
    res = _bt(config(), tiny_dataset, ml_strategies=sids, ml_models=models, ml_research=True)
    assert res.trades, "REDUCE still trades"
    base_qty = {(t.strategy_id, t.created_at): t.quantity for t in baseline.trades}
    for t in res.trades:
        assert t.ml_confidence == pytest.approx(0.62)
        assert t.initial_stop_loss > 0 and t.stop_loss > 0  # stops untouched by ML
        if (t.strategy_id, t.created_at) in base_qty:
            assert t.quantity <= base_qty[(t.strategy_id, t.created_at)]
    assert not res.observations.open_after_close


def test_backtest_with_ml_enabled_but_no_models_refuses_entries(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    res = _bt(config(), tiny_dataset, ml_strategies=tuple(baseline.strategies))
    assert res.trades == []


def test_bot_refuses_synthetic_models_without_research_flag(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    sids = tuple(baseline.strategies)
    models = {s: StrategyModels(s, stub(s, StubFilter(0.9))) for s in sids}
    with pytest.raises(SyntheticModelError):
        _bt(config(), tiny_dataset, ml_strategies=sids, ml_models=models, ml_research=False)
