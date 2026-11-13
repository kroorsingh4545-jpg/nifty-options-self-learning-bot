"""Owner decision 2026-11-13: PAPER per-trade risk Rs 3,000; LIVE stays Rs 2,000.
And W5: open trades' stop-risk counts against the daily loss limits, so open
trades together can never exceed them (per strategy and global).

All prices here are SYNTHETIC.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from config import hard_limits as hl
from config.loader import load_config
from config.settings import BotConfig
from core.enums import TradingMode
from learning.mutation_engine import NEVER_MUTABLE, WHITELIST, MutationEngine, MutationRefused, dump_strategy
from risk.daily_loss_guard import DailyLossGuard
from risk.risk_manager import OpenExposure, open_stop_risk, stop_risk
from tests.stack_harness import CE, PE, Harness, at

ROOT = Path(__file__).resolve().parents[2]
CFG = load_config(use_default_env_file=False)


# --------------------------------------------------------------------------- #
# Rs 3,000 for PAPER, Rs 2,000 for LIVE
# --------------------------------------------------------------------------- #

def test_paper_per_trade_risk_is_3000_and_the_daily_limits_are_unchanged() -> None:
    assert hl.MAX_RISK_PER_TRADE_RS_PAPER == 3_000 and hl.MAX_RISK_PER_TRADE_PCT_PAPER == 0.03
    assert hl.MAX_RISK_PER_TRADE_RS_LIVE == 2_000 and hl.MAX_RISK_PER_TRADE_PCT_LIVE == 0.02
    assert CFG.mode is TradingMode.PAPER
    assert CFG.risk.per_trade.max_risk_per_trade == 3_000
    assert {s.sizing.risk_per_trade for s in CFG.strategies.strategies.values()} == {3_000}
    assert CFG.risk.per_strategy.max_daily_loss == 5_000 == hl.MAX_DAILY_LOSS_PER_STRATEGY_RS
    assert CFG.risk.global_.max_daily_loss == 10_000 == hl.MAX_GLOBAL_DAILY_LOSS_RS
    assert {s.risk.max_daily_loss for s in CFG.strategies.strategies.values()} == {5_000}


def _as_live(cfg: BotConfig) -> dict[str, Any]:
    return {**dict(cfg), "env": cfg.env.model_copy(update={"trading_mode": TradingMode.LIVE})}


def test_live_refuses_a_per_trade_risk_above_2000() -> None:
    with pytest.raises(ValueError, match="re-decided by the owner before LIVE"):
        BotConfig.model_validate(_as_live(CFG))


def test_live_refuses_a_strategy_sizing_above_2000_even_with_the_limit_at_2000() -> None:
    risk = CFG.risk.model_copy(update={"per_trade": CFG.risk.per_trade.model_copy(
        update={"max_risk_per_trade": 2_000.0})})
    with pytest.raises(ValueError, match=r"strategies\.orb_options\.sizing\.risk_per_trade 3,000"):
        BotConfig.model_validate({**_as_live(CFG), "risk": risk})


def test_live_accepts_2000() -> None:
    risk = CFG.risk.model_copy(update={"per_trade": CFG.risk.per_trade.model_copy(
        update={"max_risk_per_trade": 2_000.0})})
    strategies = CFG.strategies.model_copy(update={"strategies": {
        sid: s.model_copy(update={"sizing": s.sizing.model_copy(update={"risk_per_trade": 2_000.0})})
        for sid, s in CFG.strategies.strategies.items()}})
    BotConfig.model_validate({**_as_live(CFG), "risk": risk, "strategies": strategies})._live_per_trade_ceiling()


def test_the_loader_still_refuses_live_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.loader import LiveModeNotPermittedError

    monkeypatch.setenv("TRADING_MODE", "LIVE")
    with pytest.raises(LiveModeNotPermittedError):
        load_config(use_default_env_file=False)


# --------------------------------------------------------------------------- #
# The learning loop can never change a risk limit
# --------------------------------------------------------------------------- #

def test_no_risk_limit_is_on_the_learning_whitelist() -> None:
    for path in WHITELIST:
        assert not path.startswith(("risk", "sizing.risk_per_trade", "allocated_capital", "per_trade"))
    for must in ("sizing.risk_per_trade", "risk", "per_trade", "per_strategy", "global", "capital"):
        assert must in NEVER_MUTABLE


@pytest.mark.parametrize("value", [2_000, 3_500, 2_999])
def test_the_learning_loop_cannot_change_the_per_trade_risk(value: int) -> None:
    engine = MutationEngine(CFG)
    with pytest.raises(MutationRefused, match="never mutable"):
        engine.propose("vwap_ema", {"sizing.risk_per_trade": value})
    deployed = dump_strategy(CFG.strategies.strategies["vwap_ema"])
    tampered = deployed | {"sizing": dict(deployed["sizing"], risk_per_trade=value)}
    with pytest.raises(MutationRefused):
        engine.apply_overlay("vwap_ema", tampered)


def test_learning_and_ml_code_never_writes_hard_limits() -> None:
    writes = re.compile(r"\b(hard_limits|hl)\.[A-Z_]+\s*=(?!=)|setattr\(\s*(hard_limits|hl)\b")
    offenders = [f"{p.relative_to(ROOT)}:{n}" for pkg in ("learning", "ml", "runtime", "strategies")
                 for p in (ROOT / pkg).rglob("*.py")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if writes.search(line)]
    assert offenders == []


# --------------------------------------------------------------------------- #
# W5: stop-risk of open trades
# --------------------------------------------------------------------------- #

def exposure(**kw: Any) -> OpenExposure:
    base: dict[str, Any] = {"strategy_id": "vwap_ema", "quantity": 65, "entry_price": 196.25, "stop": 157.0,
                            "tick_size": 0.05}
    return OpenExposure(**(base | kw))


def test_stop_risk_is_quantity_times_distance_plus_slippage_and_charges() -> None:
    assert stop_risk(exposure(), slippage_ticks=1) == pytest.approx(65 * 39.30)
    assert stop_risk(exposure(), slippage_ticks=1, cost_fn=lambda q, e, x: 70.0) == pytest.approx(65 * 39.30 + 70)
    assert stop_risk(exposure(last_price=150.0), slippage_ticks=0) == pytest.approx(65 * 46.25)  # gapped below
    assert stop_risk(exposure(stop=200.0), slippage_ticks=1) == 0.0  # trailed into profit: never negative
    assert stop_risk(exposure(quantity=0), slippage_ticks=1) == 0.0
    by = open_stop_risk([exposure(), exposure(), exposure(strategy_id="orb_options")], slippage_ticks=1)
    assert by == {"vwap_ema": pytest.approx(2 * 65 * 39.30), "orb_options": pytest.approx(65 * 39.30)}


def _guard(global_limit: float | None = None) -> DailyLossGuard:
    risk = CFG.risk
    if global_limit is not None:
        risk = risk.model_copy(update={"global_": risk.global_.model_copy(update={"max_daily_loss": global_limit})})
    return DailyLossGuard(risk, {k: v.risk for k, v in CFG.strategies.strategies.items()}, now_fn=lambda: at(10, 0))


def test_open_stop_risk_is_counted_as_already_lost_per_strategy() -> None:
    g = _guard()
    assert g.remaining_risk_budget("vwap_ema") == 3_000
    assert g.remaining_risk_budget("vwap_ema", open_risk={"vwap_ema": 2_600.0}) == pytest.approx(2_400)
    assert g.remaining_risk_budget("vwap_ema", open_risk={"vwap_ema": 5_200.0}) == 0.0
    g.record_trade_close("vwap_ema", -1_500.0)
    assert g.remaining_risk_budget("vwap_ema", open_risk={"vwap_ema": 2_000.0}) == pytest.approx(1_500)
    # the other strategy's open risk does not use this strategy's limit (only the global one)
    assert g.remaining_risk_budget("orb_options", open_risk={"vwap_ema": 2_000.0}) == 3_000


def test_open_stop_risk_counts_globally_across_strategies() -> None:
    g = _guard(global_limit=6_000)
    g.record_trade_close("orb_options", -2_000.0)
    room = g.remaining_risk_budget("vwap_ema", open_risk={"orb_options": 1_500.0, "vwap_ema": 1_000.0})
    assert room == pytest.approx(6_000 - 2_000 - 1_500 - 1_000)


def test_a_worse_mark_to_market_still_wins() -> None:
    g = _guard()
    g.update_unrealized("vwap_ema", -3_000.0)  # open trades already down more than their stop-risk says
    assert g.remaining_risk_budget("vwap_ema", open_risk={"vwap_ema": 1_000.0}) == pytest.approx(2_000)


# --------------------------------------------------------------------------- #
# W5 end to end: two concurrent trades through the real risk gate
# --------------------------------------------------------------------------- #

async def test_a_second_concurrent_trade_cannot_push_the_strategy_past_its_daily_limit() -> None:
    h = Harness(at(10, 0))
    first = await h.enter("vwap_ema", CE, ltp=190.0)  # ask 190.05, stop 152.05: ~Rs 2,920 at risk
    first_risk = stop_risk(h.s.positions.open_exposures()[0], slippage_ticks=1,
                           cost_fn=h.s.risk._cost_fn)
    assert 2_800 < first_risk <= 3_000

    h.tick(PE.instrument_key, 190.0)
    second = await h.decide("vwap_ema", PE)  # would need ~Rs 2,920 more: only ~Rs 2,080 left
    assert not second.approved
    assert any("daily loss room" in r and "open trades' stop-risk" in r for r in second.reasons), second.reasons
    assert first.open_qty == 75


async def test_two_concurrent_trades_that_fit_stay_within_the_daily_limit_together() -> None:
    h = Harness(at(10, 0))
    await h.enter("vwap_ema", CE, ltp=150.0)
    h.tick(PE.instrument_key, 150.0)
    second = await h.decide("vwap_ema", PE)
    assert second.approved, second.reasons
    open_risk = open_stop_risk(h.s.positions.open_exposures(), slippage_ticks=1, cost_fn=h.s.risk._cost_fn)
    assert second.sizing is not None
    assert second.risk_budget == pytest.approx(5_000 - open_risk["vwap_ema"])
    assert open_risk["vwap_ema"] + second.sizing.risk_amount <= 5_000


async def test_concurrent_trades_of_two_strategies_respect_the_global_limit() -> None:
    risk = CFG.risk.model_copy(update={"global_": CFG.risk.global_.model_copy(update={"max_daily_loss": 5_000.0})})
    h = Harness(at(10, 0), cfg=CFG.model_copy(update={"risk": risk}))
    await h.enter("vwap_ema", CE, ltp=190.0)  # ~Rs 2,920 at risk
    h.tick(PE.instrument_key, 150.0)
    d = await h.decide("orb_options", PE)  # orb's own limit has room; the global one does not
    assert not d.approved and any("daily loss room" in r for r in d.reasons), d.reasons


async def test_an_entry_still_filling_counts_in_full() -> None:
    h = Harness(at(10, 0))
    trade = await h.enter("vwap_ema", CE, ltp=190.0)
    pending = dataclasses.replace(trade, trade_uid="T-pending", filled_qty=0, exited_qty=0, entry_complete=False,
                                  avg_entry=0.0, tasks=set())
    h.s.positions.trades[pending.trade_uid] = pending
    (_, filling) = sorted(h.s.positions.open_exposures(), key=lambda e: e.entry_price)
    assert filling.quantity == trade.requested_qty
    assert filling.entry_price == pytest.approx(trade.levels.entry + 2 * CE.tick_size)  # the worst limit price


async def test_a_stop_trailed_into_profit_frees_the_room() -> None:
    h = Harness(at(10, 0))
    trade = await h.enter("vwap_ema", CE, ltp=190.0)
    trade.trail.stop = trade.avg_entry + 5.0
    h.tick(PE.instrument_key, 190.0)
    d = await h.decide("vwap_ema", PE)
    assert d.approved, d.reasons


async def test_unknown_open_trade_risk_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(at(10, 0))

    def broken() -> list[OpenExposure]:
        raise RuntimeError("positions unavailable")

    monkeypatch.setattr(h.s.positions, "open_exposures", broken)
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide("vwap_ema", CE)
    assert not d.approved and d.reasons == ("open trades' stop-risk unavailable",)
