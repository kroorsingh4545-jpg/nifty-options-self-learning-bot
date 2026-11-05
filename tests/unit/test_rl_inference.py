"""RL trade manager in the bot (Phase 8): advice contract, execution-side
re-validation, the advisor's fail-closed behaviour, and backtests through the
real TradingBot. All models are stubs or SYNTHETIC research models."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from backtest.backtester import Backtester, BacktestOptions, with_rl_enabled
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import BotConfig, StrategyDefaults
from core.clock import IST
from core.enums import ModelDataSource, ModelType, OptionType, OrderPurpose, OrderStatus, TradeStatus, TradingMode
from core.interfaces import ChainedFilter, FilterDecision, TradeAdvice, TradeAdviceAction
from core.models import Signal
from data.storage.records import ModelRunRecord
from ml.feature_engineering import FeatureEngine, SessionTimes
from ml.inference_service import InferenceConfigError, RLTradeAdvisor, TradeContext
from ml.model_registry import LoadedModel, SyntheticModelError
from ml.rl_environment import OBS_NAMES, AccountView, Action, PositionView
from risk.kill_switch import KillReason
from risk.stop_loss_manager import ExitReason
from tests.ml_support import config
from tests.stack_harness import CE, Harness, at
from tests.unit.test_ml_features import synthetic_day

INDEX = "NSE_INDEX|Nifty 50"
D = date(2026, 11, 4)


def rl_cfg(*sids: str, stop_pct: float | None = None, base: BotConfig | None = None) -> BotConfig:
    cfg = base or config()
    data = {sid: s.model_dump() for sid, s in cfg.strategies.strategies.items()}
    for sid in sids:
        data[sid]["rl"]["enabled"] = True
        if stop_pct is not None:
            data[sid]["stop_loss"] = {"mode": "PREMIUM_PERCENT", "value": stop_pct}
    return BotConfig(project_root=cfg.project_root, settings=cfg.settings, risk=cfg.risk, env=cfg.env,
                     strategies=StrategyDefaults.model_validate({"strategies": data}))


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


def test_trade_advice_contract() -> None:
    TradeAdvice("T", "s", TradeAdviceAction.TIGHTEN_STOP, new_stop=90.0)
    TradeAdvice("T", "s", TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1)
    TradeAdvice("T", "s", TradeAdviceAction.EXIT_FULL)
    for bad in (dict(action=TradeAdviceAction.TRAIL_STOP), dict(action=TradeAdviceAction.HOLD, new_stop=1.0),
                dict(action=TradeAdviceAction.TAKE_PARTIAL_PROFIT), dict(action=TradeAdviceAction.EXIT_FULL,
                                                                         exit_lots=1),
                dict(action=TradeAdviceAction.HOLD, confidence=1.5)):
        with pytest.raises(ValueError):
            TradeAdvice("T", "s", **bad)  # type: ignore[arg-type]
    names = {a.name for a in TradeAdviceAction}
    assert names == {"HOLD", "TIGHTEN_STOP", "MOVE_STOP_TO_BREAKEVEN", "TRAIL_STOP", "TAKE_PARTIAL_PROFIT",
                     "EXIT_FULL"}


@dataclass
class Fixed:
    d: FilterDecision
    calls: int = 0

    def evaluate(self, signal: Signal) -> FilterDecision:
        self.calls += 1
        return self.d


class Boom:
    def evaluate(self, signal: Signal) -> FilterDecision:
        raise RuntimeError("x")


def _sig(sid: str = "orb_options", t: datetime | None = None) -> Signal:
    t = t or datetime(2026, 11, 4, 10, 30, tzinfo=IST)
    return Signal(signal_id=f"SIG-{sid}", strategy_id=sid, strategy_version="0.1.0", option_type=OptionType.CE,
                  generated_at=t, candle_end=t, underlying_key=INDEX, underlying_price=25_000.0, reason="test")


def test_chained_filter_can_only_veto_or_shrink() -> None:
    a = Fixed(FilterDecision(True, 0.7, 0.5, "ml reduce"))
    b = Fixed(FilterDecision(True, None, 1.0, "rl enter"))
    d = ChainedFilter(a, b).evaluate(_sig())
    assert d.allow and d.size_multiplier == 0.5 and d.score == 0.7
    veto = Fixed(FilterDecision(False, None, 0.0, "rl skip"))
    assert not ChainedFilter(a, veto).evaluate(_sig()).allow
    first_veto = Fixed(FilterDecision(False, 0.1, 0.0, "ml reject"))
    later = Fixed(FilterDecision(True))
    assert not ChainedFilter(first_veto, later).evaluate(_sig()).allow and later.calls == 0
    assert not ChainedFilter(a, Boom()).evaluate(_sig()).allow  # an error vetoes


# --------------------------------------------------------------------------- #
# Execution: apply_advice re-validates everything
# --------------------------------------------------------------------------- #


def harness(stop_pct: float = 10.0, now: datetime | None = None) -> Harness:
    return Harness(now=now, cfg=rl_cfg("orb_options", stop_pct=stop_pct))


def adv(t: Any, action: TradeAdviceAction, **kw: Any) -> TradeAdvice:
    return TradeAdvice(t.trade_uid, t.strategy_id, action, **kw)


def protective(h: Harness, uid: str) -> list[Any]:
    return [o for o in h.repos.orders.list_for_trade(uid) if o.purpose is OrderPurpose.STOP_LOSS
            and o.status not in (OrderStatus.CANCELLED, OrderStatus.COMPLETE, OrderStatus.REJECTED)]


async def test_tighten_moves_stop_and_the_resting_order() -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    assert t.open_qty == 2 * CE.lot_size
    h.tick(CE.instrument_key, 120.0)
    old = t.stop  # the strategy's own trailing stop may already have moved
    new = round(old + 2.0, 2)
    assert new < 120.0
    res = h.s.positions.apply_advice(adv(t, TradeAdviceAction.TRAIL_STOP, new_stop=new))
    await h.settle()
    assert res.applied and t.stop == new > old
    assert h.repos.trades.get(t.trade_uid).stop_loss == new  # type: ignore[union-attr]
    po = protective(h, t.trade_uid)
    assert len(po) == 1 and po[0].trigger_price == new


@pytest.mark.parametrize("why,make", [
    ("widen", lambda t: adv(t, TradeAdviceAction.TIGHTEN_STOP, new_stop=t.stop - 3.0)),
    ("at market", lambda t: adv(t, TradeAdviceAction.TIGHTEN_STOP, new_stop=120.0)),
    ("above market", lambda t: adv(t, TradeAdviceAction.TRAIL_STOP, new_stop=125.0)),
    ("other strategy", lambda t: TradeAdvice(t.trade_uid, "vwap_ema", TradeAdviceAction.EXIT_FULL)),
    ("unknown trade", lambda t: TradeAdvice("NOPE", t.strategy_id, TradeAdviceAction.EXIT_FULL)),
    ("all lots", lambda t: adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=2)),
])
async def test_unsafe_advice_is_refused(why: str, make: Callable[[Any], TradeAdvice]) -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 120.0)
    stop, qty = t.stop, t.open_qty
    res = h.s.positions.apply_advice(make(t))
    await h.settle()
    assert not res.applied, why
    assert t.stop == stop and t.open_qty == qty and t.status is TradeStatus.OPEN


async def test_advice_is_refused_when_rl_is_off_in_square_off_or_while_exiting() -> None:
    h = Harness(cfg=config())  # rl.enabled: false
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 120.0)
    assert not h.s.positions.apply_advice(adv(t, TradeAdviceAction.EXIT_FULL)).applied
    h2 = harness()
    t2 = await h2.enter(ltp=100.0)
    h2.tick(CE.instrument_key, 120.0)
    h2.wall.now = at(15, 10)
    r = h2.s.positions.apply_advice(adv(t2, TradeAdviceAction.TRAIL_STOP, new_stop=t2.stop + 1))
    assert not r.applied and "square-off" in r.reason
    h3 = harness()
    t3 = await h3.enter(ltp=100.0)
    h3.tick(CE.instrument_key, 120.0)
    t3.exit_in_progress = True
    assert not h3.s.positions.apply_advice(adv(t3, TradeAdviceAction.TRAIL_STOP, new_stop=t3.stop + 1)).applied


async def test_full_exit_is_labelled_trade_manager() -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 112.0)
    assert h.s.positions.apply_advice(adv(t, TradeAdviceAction.EXIT_FULL)).applied
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == ExitReason.TRADE_MANAGER.value
    assert not protective(h, t.trade_uid)


async def test_partial_profit_keeps_a_lot_and_its_protection() -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 98.0)
    r = h.s.positions.apply_advice(adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1))
    assert not r.applied and "profit" in r.reason  # only in profit
    h.tick(CE.instrument_key, 120.0)
    assert h.s.positions.apply_advice(adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1)).applied
    await h.settle()
    assert t.status is TradeStatus.OPEN and t.open_qty == CE.lot_size and t.partial_exits == 1
    assert t.exit_reason is None and not t.exit_in_progress
    po = protective(h, t.trade_uid)
    assert len(po) == 1 and po[0].quantity == CE.lot_size  # protective stop re-placed for what is left
    r = h.s.positions.apply_advice(adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1))
    assert not r.applied  # max_partial_exits (1) and at least one lot stays
    h.tick(CE.instrument_key, t.stop - 1)  # the stop still protects the rest
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason in ("STOP_LOSS", "TRAILING_STOP")


async def test_a_stop_hit_during_a_partial_exit_exits_the_rest() -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 120.0)
    assert h.s.positions.apply_advice(adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1)).applied
    assert t.partial_in_progress
    h.tick(CE.instrument_key, t.stop - 5)  # before the partial-exit task ran
    assert t.escalate_exit is ExitReason.STOP_LOSS
    await h.settle()
    assert t.status is TradeStatus.CLOSED and t.open_qty == 0


async def test_kill_switch_during_a_partial_exit_flattens_everything() -> None:
    h = harness()
    t = await h.enter(ltp=100.0)
    h.tick(CE.instrument_key, 120.0)
    assert h.s.positions.apply_advice(adv(t, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=1)).applied
    h.s.kill_switch.trigger(KillReason.MANUAL, "test")
    await h.settle()
    assert t.status is TradeStatus.CLOSED and t.open_qty == 0


# --------------------------------------------------------------------------- #
# The advisor
# --------------------------------------------------------------------------- #


@dataclass
class StubRL:
    """A fixed-action stand-in for a PPO trade manager (NOT a trained model)."""

    action: Action
    fail: bool = False
    seen: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)

    def act(self, raw: np.ndarray, mask: np.ndarray) -> tuple[Action, dict[str, Any]]:
        if self.fail:
            raise RuntimeError("policy exploded")
        self.seen.append((raw, mask))
        assert raw.shape == (len(OBS_NAMES),)
        a = self.action if mask[self.action.value] else Action.HOLD
        return a, {"confidence": 0.9}


def rl_stub(sid: str, model: Any, source: ModelDataSource = ModelDataSource.SYNTHETIC, kind: str = "rl_trade_manager",
            owner: str | None = None) -> LoadedModel:
    rec = ModelRunRecord(run_id=f"RUN-{sid}-rl", model_type=ModelType.RL_PPO, model_version=f"stub-rl-{sid}-synthetic",
                         strategy_id=owner or sid, feature_schema_version="1.0.0", feature_schema_json={},
                         training_start=datetime(2026, 1, 1, tzinfo=IST), training_end=datetime(2026, 2, 1, tzinfo=IST),
                         artifact_path="-", data_source=source)
    return LoadedModel(rec, {"model_kind": kind, "synthetic": source is not ModelDataSource.HISTORICAL}, model,
                       Path("-"))


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def advisor(models: dict[str, LoadedModel], *, research: bool = True, cfg: BotConfig | None = None,
            feed_until: datetime | None = None) -> tuple[RLTradeAdvisor, Clock]:
    clock = Clock(datetime(2026, 11, 4, 10, 30, tzinfo=IST))
    eng = FeatureEngine(index_key=INDEX, times=SessionTimes())
    for c in synthetic_day(D):
        if c.end <= (feed_until or clock.now):
            eng.on_candle(c)
    acct = AccountView(0, 0.0, 5000.0, 0, 4, False, 0, 2000.0)
    a = RLTradeAdvisor(cfg or rl_cfg("orb_options"), features=eng, models=models, option_context=lambda s, t: None,
                       account=lambda sid: acct, outcomes=lambda sid: (), research=research, now_fn=clock)
    return a, clock


def ctx(sid: str = "orb_options", price: float = 120.0, lots: int = 2) -> TradeContext:
    pos = PositionView(OptionType.CE, 100.0, 90.0, 90.0, 120.0 + 100, 65, lots, lots,
                       datetime(2026, 11, 4, 10, 0, tzinfo=IST), price)
    acct = AccountView(1, 0.0, 5000.0, 1, 4, False, 0, 2000.0)
    return TradeContext(sid, "T-1", pos, None, price, 0.05, acct)


def test_advisor_model_policy() -> None:
    m = rl_stub("orb_options", StubRL(Action.HOLD))
    with pytest.raises(SyntheticModelError):
        advisor({"orb_options": m}, research=False)
    with pytest.raises(InferenceConfigError):
        RLTradeAdvisor(rl_cfg("orb_options"), features=FeatureEngine(index_key=INDEX, times=SessionTimes()),
                       models={}, option_context=lambda s, t: None, account=lambda s: None,  # type: ignore[arg-type,return-value]
                       outcomes=lambda s: (), mode=TradingMode.LIVE, research=True)
    with pytest.raises(InferenceConfigError):
        advisor({"orb_options": rl_stub("orb_options", StubRL(Action.HOLD), owner="vwap_ema")})
    with pytest.raises(InferenceConfigError):
        advisor({"orb_options": rl_stub("orb_options", StubRL(Action.HOLD), kind="signal_filter")})


def test_entry_approval_can_only_veto() -> None:
    enter, skip = StubRL(Action.ENTER), StubRL(Action.SKIP_SETUP)
    a, _ = advisor({"orb_options": rl_stub("orb_options", enter)})
    d = a.evaluate(_sig())
    assert d.allow and d.size_multiplier == 1.0
    assert enter.seen[-1][1].tolist() == [False] * 6 + [True, True]  # only ENTER / SKIP offered
    a, _ = advisor({"orb_options": rl_stub("orb_options", skip)})
    assert not a.evaluate(_sig()).allow
    a, _ = advisor({})
    assert not a.evaluate(_sig()).allow  # RL enabled, no model: fail-closed
    assert a.evaluate(_sig("vwap_ema")).allow  # RL off for vwap_ema: pass-through
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.ENTER, fail=True))})
    assert not a.evaluate(_sig()).allow
    a, clock = advisor({"orb_options": rl_stub("orb_options", enter)},
                       feed_until=datetime(2026, 11, 4, 10, 20, tzinfo=IST))
    assert not a.evaluate(_sig()).allow  # stale features


def test_advice_is_bounded_and_fails_to_hold() -> None:
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.TRAIL_STOP))})
    out = a.advise(ctx())
    assert out.action is TradeAdviceAction.TRAIL_STOP and out.new_stop == 115.0  # 120 - 0.5R (R = 10)
    assert a.advise(ctx(price=92.0)).action is TradeAdviceAction.HOLD  # would loosen -> HOLD
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.TAKE_PARTIAL_PROFIT))})
    assert a.advise(ctx()).exit_lots == 1
    assert a.advise(ctx(lots=1)).action is TradeAdviceAction.HOLD
    a, _ = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.EXIT_FULL, fail=True))})
    assert a.advise(ctx()).action is TradeAdviceAction.HOLD
    a, clock = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.EXIT_FULL))})
    assert a.advise(ctx()).action is TradeAdviceAction.EXIT_FULL
    clock.now = datetime(2026, 11, 4, 15, 10, tzinfo=IST)
    assert a.advise(ctx()).action is TradeAdviceAction.HOLD  # square-off belongs to the square-off manager
    stub = StubRL(Action.ENTER)
    a, _ = advisor({"orb_options": rl_stub("orb_options", stub)})
    assert a.advise(ctx()).action is TradeAdviceAction.HOLD
    assert stub.seen[-1][1].tolist() == [True] * 6 + [False, False]  # ENTER is never offered in a trade


def test_decision_interval() -> None:
    a, clock = advisor({"orb_options": rl_stub("orb_options", StubRL(Action.HOLD))})
    assert a.due("T-1", "orb_options", clock.now)
    a.advise(ctx())
    assert not a.due("T-1", "orb_options", clock.now + timedelta(seconds=20))
    assert a.due("T-1", "orb_options", clock.now + timedelta(minutes=1))


# --------------------------------------------------------------------------- #
# Backtests through the real bot
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


def test_backtest_with_rl_enabled_but_no_models_refuses_entries(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    assert baseline.closed_trades()
    res = _bt(config(), tiny_dataset, rl_strategies=tuple(baseline.strategies))
    assert res.trades == []


def test_backtest_refuses_synthetic_rl_models_without_research_flag(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    sids = tuple(baseline.strategies)
    models = {s: rl_stub(s, StubRL(Action.ENTER)) for s in sids}
    with pytest.raises(SyntheticModelError):
        _bt(config(), tiny_dataset, rl_strategies=sids, rl_models=models, rl_research=False)


def test_backtest_skip_everything_takes_no_trades(tiny_dataset, baseline) -> None:  # type: ignore[no-untyped-def]
    sids = tuple(baseline.strategies)
    models = {s: rl_stub(s, StubRL(Action.SKIP_SETUP)) for s in sids}
    res = _bt(config(), tiny_dataset, rl_strategies=sids, rl_models=models, rl_research=True)
    assert res.trades == []
    reasons = {r for c in res.observations.rejections.values() for r in c}
    assert any("RL SKIP_SETUP" in r for r in reasons)


@pytest.mark.parametrize("action", [Action.TRAIL_STOP, Action.EXIT_FULL, Action.TAKE_PARTIAL_PROFIT])
def test_backtest_rl_management_keeps_every_risk_control(tiny_dataset, baseline, action: Action) -> None:  # type: ignore[no-untyped-def]
    sids = tuple(baseline.strategies)

    @dataclass
    class EnterThen(StubRL):
        def act(self, raw: np.ndarray, mask: np.ndarray) -> tuple[Action, dict[str, Any]]:
            return (Action.ENTER if mask[Action.ENTER.value] else
                    (self.action if mask[self.action.value] else Action.HOLD)), {"confidence": 1.0}

    models = {s: rl_stub(s, EnterThen(action)) for s in sids}
    res = _bt(config(), tiny_dataset, rl_strategies=sids, rl_models=models, rl_research=True)
    assert res.trades, "entries are approved"
    sq = config().risk.time_rules.square_off_start
    for t in res.trades:
        assert t.initial_stop_loss > 0 and t.stop_loss >= t.initial_stop_loss  # never loosened
        if t.exit_time is not None:
            assert t.exit_time.time() <= config().risk.time_rules.square_off_end
        assert t.entry_time is None or t.entry_time.time() < config().risk.time_rules.no_new_entries_after
    assert not res.observations.open_after_close
    if action is Action.EXIT_FULL:
        assert any(t.exit_reason == "TRADE_MANAGER" for t in res.trades)
    if action is Action.TRAIL_STOP:
        assert any(t.stop_loss > t.initial_stop_loss for t in res.trades)
    assert sq is not None


def test_with_rl_enabled_only_flips_the_flag() -> None:
    cfg = with_rl_enabled(config(), ["orb_options"])
    s0, s1 = config().strategies.strategies["orb_options"], cfg.strategies.strategies["orb_options"]
    assert s1.rl.enabled and s1.rl.model_copy(update={"enabled": False}) == s0.rl
    assert cfg.risk == config().risk


def test_rl_runtime_never_touches_risk_state() -> None:
    """The RL wiring reads the daily guard and the kill switch, and only ever
    acts through PositionManager.apply_advice."""
    import ast

    root = Path(__file__).resolve().parents[2]
    forbidden = {"trigger", "reset", "record_entry", "record_trade_close", "update_unrealized", "set_status",
                 "exit_all", "exit_trade", "request_exit", "submit", "cancel", "modify_trigger", "place_order"}
    for rel in ("runtime/rl.py", "ml/rl_agent.py", "ml/rl_environment.py", "ml/reward_design.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        bad = forbidden - ({"reset"} if rel.startswith("ml/") else set())  # ml: gymnasium env.reset()
        assert not calls & bad, (rel, calls & bad)
    rt = (root / "runtime/rl.py").read_text(encoding="utf-8")
    assert "apply_advice(" in rt and "credential" not in rt.lower() and "token" not in rt.lower()
