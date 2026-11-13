"""RiskManager pre-trade gate and kill-switch monitors."""

from __future__ import annotations

import asyncio

import pytest

from core.enums import StrategyStatus
from risk.kill_switch import KillReason
from tests.stack_harness import CE, PE, Harness, at


async def test_approves_valid_entry_with_full_risk_detail() -> None:
    h = Harness(at(10, 0))
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    assert d.approved, d.reasons
    assert d.entry_price == pytest.approx(100.05)  # ask
    assert d.levels is not None and d.levels.stop == 75.05  # 25% stop, rounded up
    assert d.levels.target is not None and d.levels.target > d.entry_price
    assert d.sizing is not None and d.quantity == 75
    assert d.sizing.risk_amount <= 3000
    assert d.risk_budget == 3000  # PAPER per-trade risk (owner decision 2026-11-13)


@pytest.mark.parametrize(("now", "sid", "reason"), [
    (at(9, 20), "orb_options", "entry window"),        # before 09:30
    (at(11, 0), "orb_options", "entry window"),        # ORB window ends 11:00
    (at(14, 45), "vwap_ema", "no new entries after"),  # global cutoff 14:45
    (at(15, 12), "vwap_ema", "square-off"),
    (at(9, 0), "vwap_ema", "market closed"),
])
async def test_time_rules(now: object, sid: str, reason: str) -> None:
    h = Harness(now)  # type: ignore[arg-type]
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide(sid)
    assert not d.approved and any(reason in r for r in d.reasons), d.reasons


async def test_holiday_blocks_entries_in_the_risk_gate() -> None:
    from datetime import datetime

    from core.clock import IST

    h = Harness(datetime(2026, 11, 10, 10, 0, tzinfo=IST))  # Diwali Balipratipada (NSE holiday), a Tuesday
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide("vwap_ema")
    assert not d.approved and any("not an NSE trading day" in r for r in d.reasons), d.reasons


async def test_missing_holiday_calendar_for_the_year_blocks_entries() -> None:
    from datetime import datetime

    from core.clock import IST

    h = Harness(datetime(2027, 1, 5, 10, 0, tzinfo=IST))  # a Tuesday; no 2027 holidays configured
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide("vwap_ema")
    assert not d.approved and any("no NSE holiday calendar configured for 2027" in r for r in d.reasons), d.reasons


async def test_kill_switch_blocks_entries() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    h.s.kill_switch.trigger(KillReason.MANUAL, "test", strategy_id="orb_options")
    assert not (await h.decide("orb_options")).approved
    assert (await h.decide("vwap_ema")).approved
    h.s.kill_switch.trigger(KillReason.MANUAL, "global")
    d = await h.decide("vwap_ema")
    assert not d.approved and "kill switch" in d.reasons[0]


async def test_strategy_status_must_be_active() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    h.repos.strategies.set_status("orb_options", StrategyStatus.PAUSED, at=h.wall())
    d = await h.decide()
    assert not d.approved and any("PAUSED" in r for r in d.reasons)


async def test_stale_or_missing_quote_rejected() -> None:
    h = Harness()
    assert not (await h.decide()).approved  # no quote
    h.tick(CE.instrument_key, 100.0)
    h.wall.advance(seconds=30)
    d = await h.decide()
    assert not d.approved and "fresh" in d.reasons[0]
    h.md.mark_all_stale()
    h.wall.advance(seconds=-30)
    assert not (await h.decide()).approved


async def test_wide_spread_rejected() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0, spread=3.0)  # 3% > 1.5%
    d = await h.decide()
    assert not d.approved and "spread" in d.reasons[0]


async def test_premium_bounds() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 20.0, spread=0.1)
    assert not (await h.decide()).approved
    h.tick(CE.instrument_key, 450.0, spread=0.1)
    assert not (await h.decide()).approved


async def test_open_trade_caps_per_strategy_and_total() -> None:
    h = Harness()
    await h.enter("orb_options", CE)
    await h.enter("orb_options", PE)
    d = await h.decide("orb_options", CE)
    assert not d.approved and any("strategy open-trade cap" in r for r in d.reasons)
    await h.enter("vwap_ema", CE)
    await h.enter("vwap_ema", PE)
    assert h.s.positions.open_trade_count() == 4


async def test_total_cap_counted_across_strategies() -> None:
    from config.loader import load_config

    base = load_config(use_default_env_file=False)
    stricter = base.risk.model_copy(update={"global_": base.risk.global_.model_copy(update={"max_open_trades": 3})})
    h = Harness(cfg=base.model_copy(update={"risk": stricter}))
    await h.enter("orb_options", CE)
    await h.enter("orb_options", PE)
    await h.enter("vwap_ema", CE)
    # vwap_ema has room for one more (per-strategy cap 2) but the global cap of 3 binds.
    d = await h.decide("vwap_ema", PE)
    assert not d.approved
    assert any("total open-trade cap" in r for r in d.reasons)
    assert not any("strategy open-trade cap" in r for r in d.reasons)


async def test_risk_budget_shrinks_with_daily_losses() -> None:
    h = Harness()
    h.s.guard.record_trade_close("orb_options", -3500)
    h.wall.advance(minutes=16)  # past cooldown
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    assert d.risk_budget == pytest.approx(1500)
    assert not d.approved and "position size zero" in d.reasons[0]  # 1 lot at 25% stop risks ~1,950


async def test_ml_multiplier_can_veto_or_shrink_but_not_enlarge() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 40.0, spread=0.05)  # cheap premium -> several lots
    base = await h.decide("vwap_ema")
    bigger = await h.decide("vwap_ema", size_multiplier=5.0)
    smaller = await h.decide("vwap_ema", size_multiplier=0.5)
    veto = await h.decide("vwap_ema", size_multiplier=0.0)
    assert base.quantity == bigger.quantity  # clipped to 1.0
    assert smaller.quantity < base.quantity
    assert not veto.approved


async def test_option_type_mismatch_rejected() -> None:
    h = Harness()
    h.tick(PE.instrument_key, 100.0)
    from core.enums import OptionType
    from risk.risk_manager import EntryProposal

    p = EntryProposal(strategy_id="orb_options", instrument=PE, option_type=OptionType.CE, reason="x")
    assert not h.s.risk.evaluate_entry(p, available_margin=1e6).approved


# --------------------------------------------------------------------------- #
# Monitors
# --------------------------------------------------------------------------- #


class S:
    def __init__(self, v: str) -> None:
        self.value = v


async def test_websocket_failed_and_auth_failed_trigger_kill() -> None:
    h = Harness()
    h.s.risk.on_stream_state("market_data", S("RECONNECTING"), S("FAILED"))
    assert h.s.kill_switch.global_event is not None
    assert h.s.kill_switch.global_event.reason is KillReason.WEBSOCKET_DISCONNECT
    h2 = Harness()
    h2.s.risk.on_stream_state("portfolio", S("CONNECTING"), S("AUTH_FAILED"))
    assert h2.s.kill_switch.global_event is not None
    assert h2.s.kill_switch.global_event.reason is KillReason.AUTH_FAILURE
    await h.settle()
    await h2.settle()


async def test_websocket_disconnect_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    grace = h.cfg.risk.kill_switch.model_copy(update={"websocket_disconnect_grace_seconds": 0.05})
    h.s.risk._ks_cfg = grace
    h.s.risk.on_stream_state("market_data", S("CONNECTED"), S("RECONNECTING"))
    h.s.risk.on_stream_state("market_data", S("RECONNECTING"), S("CONNECTED"))  # recovered in time
    await asyncio.sleep(0.08)
    assert not h.s.kill_switch.active
    h.s.risk.on_stream_state("market_data", S("CONNECTED"), S("RECONNECTING"))
    await asyncio.sleep(0.08)
    assert h.s.kill_switch.active
    await h.settle()


async def test_abnormal_spread_on_held_contract() -> None:
    h = Harness()
    await h.enter("orb_options", CE)
    h.tick(CE.instrument_key, 100.0, spread=8.0)  # 8% > 5%
    h.tick(CE.instrument_key, 100.0, spread=8.0)
    assert not h.s.kill_switch.active
    h.tick(CE.instrument_key, 100.0, spread=0.1)  # resets the counter
    for _ in range(3):
        h.tick(CE.instrument_key, 100.0, spread=8.0)
    assert h.s.kill_switch.active
    assert h.s.kill_switch.global_event is not None
    assert h.s.kill_switch.global_event.reason is KillReason.ABNORMAL_SPREAD
    await h.settle()


async def test_wide_spread_on_unheld_contract_ignored() -> None:
    h = Harness()
    for _ in range(5):
        h.tick(PE.instrument_key, 100.0, spread=9.0)
    assert not h.s.kill_switch.active


async def test_api_failures_and_rate_limit_trip() -> None:
    h = Harness()
    for _ in range(2):
        h.s.risk.record_api_result(False)
    h.s.risk.record_api_result(True)  # success resets the count
    for _ in range(2):
        h.s.risk.record_api_result(False)
    assert not h.s.kill_switch.active
    h.s.risk.record_api_result(False)
    assert h.s.kill_switch.active
    h2 = Harness()
    h2.s.risk.on_rate_limit_trip("order_api")
    assert h2.s.kill_switch.global_event is not None
    assert h2.s.kill_switch.global_event.reason is KillReason.RATE_LIMIT_CIRCUIT
    await h.settle()
    await h2.settle()
