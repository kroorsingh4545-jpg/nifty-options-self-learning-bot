"""D2: the shadow simulator's copy of the bot's trade rules must behave exactly
like the bot's own code (SYNTHETIC inputs). Research may not import ``risk/``,
``runtime/`` or the paper broker; this test may, and compares them."""

from __future__ import annotations

import random
from datetime import date
from typing import Any

import pytest

from broker.paper_broker import _round_to_tick
from broker.upstox_mapping import parse_instrument
from config.loader import load_config
from config.settings import StopLossConfig, TakeProfitConfig, TrailingConfig
from core.enums import OptionType, OrderSide
from data.option_chain import LiquidityFilter
from research.__main__ import shadow_rules
from research.shadow.rules import TradeRules, Trail, check_exit, levels
from research.shadow.simulate import Contract, Quote, _buy_price, _sell_price, liquidity_reasons, select_contract
from risk.stop_loss_manager import check_exit as bot_check_exit
from risk.stop_loss_manager import compute_levels
from risk.trailing_stop_manager import TrailingStopManager
from runtime.contracts import OptionUniverse, quote_from_tick
from tests.fakes import option_contract
from tests.synthetic import at, tick

CFG = load_config(use_default_env_file=False)


def rules(**kw: Any) -> TradeRules:
    base: dict[str, Any] = {"stop_mode": "PREMIUM_PERCENT", "stop_value": 25.0, "target_mode": "R_MULTIPLE",
                            "target_value": 2.0, "trailing_enabled": True, "trailing_mode": "R_MULTIPLE",
                            "trailing_activation": 1.0, "trailing_step": 0.5}
    return TradeRules(**{**base, **kw})


STOP_CASES = [("PREMIUM_PERCENT", 25.0), ("PREMIUM_PERCENT", 20.0), ("POINTS", 15.0), ("POINTS", 0.5)]
TARGET_CASES = [("R_MULTIPLE", 2.0), ("R_MULTIPLE", 1.5), ("PREMIUM_PERCENT", 40.0), ("POINTS", 30.0)]


@pytest.mark.parametrize(("sl_mode", "sl_value"), STOP_CASES)
@pytest.mark.parametrize(("tp_mode", "tp_value"), TARGET_CASES)
def test_levels_match_compute_levels(sl_mode: str, sl_value: float, tp_mode: str, tp_value: float) -> None:
    r = rules(stop_mode=sl_mode, stop_value=sl_value, target_mode=tp_mode, target_value=tp_value)
    sl, tp = StopLossConfig(mode=sl_mode, value=sl_value), TakeProfitConfig(mode=tp_mode, value=tp_value)
    for entry in [0.4, 1.05, 12.35, 39.3, 81.45, 99.95, 100.05, 138.25, 157.2, 399.95, 812.6]:
        mine = levels(entry, r)
        try:
            bot = compute_levels(entry, sl, tp, 0.05)
        except ValueError:
            assert mine is None, entry
            continue
        assert mine is not None, entry
        assert (mine.stop, mine.target, mine.risk_per_unit) == (bot.stop, bot.target, bot.risk_per_unit), entry


def test_check_exit_matches_the_bot() -> None:
    rng = random.Random(3)  # noqa: S311 - seeded test data
    for _ in range(2000):
        ltp, stop = round(rng.uniform(50, 150), 2), round(rng.uniform(40, 120), 2)
        target = None if rng.random() < 0.2 else round(rng.uniform(100, 200), 2)
        bot = bot_check_exit(ltp, stop, target)
        assert check_exit(ltp, stop, target) == (None if bot is None else bot.value)


@pytest.mark.parametrize("mode,activation,step", [("R_MULTIPLE", 1.0, 0.5), ("R_MULTIPLE", 0.5, 0.25),
                                                   ("PREMIUM_PERCENT", 15.0, 5.0), ("POINTS", 10.0, 4.0)])
@pytest.mark.parametrize("seed", range(8))
def test_trailing_matches_the_bot_on_random_paths(mode: str, activation: float, step: float, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - seeded test data
    r = rules(trailing_mode=mode, trailing_activation=activation, trailing_step=step)
    entry = round(rng.uniform(60, 250), 2)
    lv = levels(entry, r)
    assert lv is not None
    bot_lv = compute_levels(entry, StopLossConfig(mode="PREMIUM_PERCENT", value=25.0),
                            TakeProfitConfig(mode="R_MULTIPLE", value=2.0), 0.05)
    mgr = TrailingStopManager(TrailingConfig(enabled=True, mode=mode, activation=activation, step=step),
                              OptionType.CE, 0.05)
    state = mgr.new_state(bot_lv.entry, bot_lv.stop)
    mine = Trail(r, lv)
    price = entry
    for _ in range(400):
        price = max(0.05, round(price * (1 + rng.gauss(0.002, 0.02)), 2))
        assert mine.update(price) == mgr.update(state, price)
        assert (mine.stop, mine.peak, mine.active) == (state.stop, state.peak, state.active)


def test_disabled_trailing_never_moves_the_stop() -> None:
    r = rules(trailing_enabled=False)
    lv = levels(100.0, r)
    assert lv is not None
    t = Trail(r, lv)
    assert not any(t.update(p) for p in (110.0, 150.0, 300.0)) and t.stop == lv.stop


@pytest.mark.parametrize("price", [0.01, 0.05, 12.341, 99.999, 100.0, 100.021, 157.24, 400.0])
def test_fill_rounding_matches_the_paper_broker(price: float) -> None:
    r = rules()
    assert _buy_price(price, r) == _round_to_tick(price + 0.05, 0.05, OrderSide.BUY)
    assert _sell_price(price, r) == _round_to_tick(price - 0.05, 0.05, OrderSide.SELL)


def _instruments() -> list[Any]:
    out = []
    for expiry in ("2026-11-10", "2026-11-17"):
        out += [parse_instrument(option_contract(k, o, expiry, key=f"NSE_FO|{expiry[-2:]}{k}{o}"))
                for k in range(24000, 25050, 50) for o in ("CE", "PE")]
    return out


@pytest.mark.parametrize("mode,offset", [("ATM", 0), ("ITM", 1), ("ITM", 3), ("OTM", 2), ("OTM", 5)])
def test_contract_selection_matches_the_bot_universe(mode: str, offset: int) -> None:
    insts = _instruments()
    near = [i for i in insts if i.expiry == date(2026, 11, 10)]
    uni = OptionUniverse(near, expiry=date(2026, 11, 10), band=5)
    contracts = [Contract(i.instrument_key, i.option_type.value, i.strike, i.expiry, i.lot_size) for i in insts]
    rng = random.Random(1)  # noqa: S311 - seeded test data
    for _ in range(300):
        spot = round(rng.uniform(24100, 24950), 2)
        for opt in (OptionType.CE, OptionType.PE):
            bot = uni.select(spot, opt, mode, offset)
            mine = select_contract(spot, opt.value, date(2026, 11, 3), contracts, mode=mode, offset=offset)
            assert (None if bot is None else bot.instrument_key) == (None if mine is None else mine.instrument_key)


def test_contract_selection_skips_expired_contracts() -> None:
    contracts = [Contract(i.instrument_key, i.option_type.value, i.strike, i.expiry, i.lot_size)
                 for i in _instruments()]
    c = select_contract(24500.0, "CE", date(2026, 11, 11), contracts)
    assert c is not None and c.expiry == date(2026, 11, 17)


@pytest.mark.parametrize("ltp,spread,vtt,oi", [(100.0, 0.1, 250_000, 1_200_000), (100.0, 4.0, 250_000, 1_200_000),
                                               (30.0, 0.1, 250_000, 1_200_000), (500.0, 0.1, 5, 10),
                                               (0.05, 0.1, 250_000, 1_200_000)])
def test_liquidity_reasons_match_the_bot_filter(ltp: float, spread: float, vtt: int, oi: float) -> None:
    ss = CFG.strategies.strategies["orb_options"].strike_selection
    inst = parse_instrument(option_contract(24500, "CE", "2026-11-10"))
    t = tick(inst.instrument_key, ltp, at(10, 0), spread=spread, vtt=vtt, oi=oi)
    bot = LiquidityFilter(ss.liquidity, min_premium=ss.min_premium,
                          max_premium=ss.max_premium).check(quote_from_tick(inst, t))
    r = shadow_rules(CFG, 0.05)["orb_options"]
    q = Quote(t.received_at, t.exchange_time, t.ltp, t.bid, t.ask, t.bid_qty, t.ask_qty, t.volume_today, t.oi)
    assert liquidity_reasons(q, r) == list(bot.reasons)


def test_rules_are_built_from_the_bot_configuration() -> None:
    for sid, st in CFG.strategies.strategies.items():
        r = shadow_rules(CFG, 0.05)[sid]
        assert (r.stop_mode, r.stop_value) == (st.stop_loss.mode, st.stop_loss.value)
        assert (r.target_mode, r.target_value) == (st.take_profit.mode, st.take_profit.value)
        assert (r.trailing_enabled, r.trailing_mode, r.trailing_activation, r.trailing_step) == (
            st.trailing.enabled, st.trailing.mode, st.trailing.activation, st.trailing.step)
        assert (r.strike_mode, r.strike_offset) == (st.strike_selection.mode, st.strike_selection.offset_steps)
        assert r.slippage_ticks == CFG.settings.paper_broker.slippage_ticks
        assert r.entry_buffer_ticks == CFG.settings.execution.entry_limit_buffer_ticks
        assert r.max_quote_age_seconds == CFG.settings.paper_broker.max_quote_age_seconds
        assert r.square_off_start == CFG.risk.time_rules.square_off_start
