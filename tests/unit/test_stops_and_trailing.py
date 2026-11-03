"""Stop-loss / target levels, exit triggers, trailing stops (all modes)."""

from __future__ import annotations

import pytest

from config.settings import StopLossConfig, TakeProfitConfig, TrailingConfig
from core.enums import OptionType
from risk.stop_loss_manager import (
    ExitReason,
    StopWideningError,
    check_exit,
    compute_levels,
    r_multiple,
    tighten_stop,
)
from risk.trailing_stop_manager import TrailingStopManager

# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #


def test_percent_stop_and_r_target() -> None:
    lv = compute_levels(100.0, StopLossConfig(mode="PREMIUM_PERCENT", value=25),
                        TakeProfitConfig(mode="R_MULTIPLE", value=2.0))
    assert lv.stop == 75.0 and lv.risk_per_unit == 25.0 and lv.target == 150.0


def test_points_stop_and_percent_target() -> None:
    lv = compute_levels(120.0, StopLossConfig(mode="POINTS", value=18),
                        TakeProfitConfig(mode="PREMIUM_PERCENT", value=40))
    assert lv.stop == 102.0 and lv.target == 168.0


def test_levels_round_to_tick_conservatively() -> None:
    lv = compute_levels(100.5, StopLossConfig(mode="PREMIUM_PERCENT", value=25),
                        TakeProfitConfig(mode="POINTS", value=10.01))
    assert lv.stop == 75.40  # 75.375 rounded UP (tighter, never more risk)
    assert lv.target == 110.55


def test_stop_points_larger_than_premium_rejected() -> None:
    with pytest.raises(ValueError):
        compute_levels(10.0, StopLossConfig(mode="POINTS", value=15), TakeProfitConfig(mode="R_MULTIPLE", value=2))


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("ltp", "expected"), [(75.0, ExitReason.STOP_LOSS), (74.0, ExitReason.STOP_LOSS),
                                               (75.05, None), (149.95, None), (150.0, ExitReason.TARGET),
                                               (180.0, ExitReason.TARGET)])
def test_exit_triggers(ltp: float, expected: ExitReason | None) -> None:
    assert check_exit(ltp, 75.0, 150.0) is expected


def test_no_target_means_only_stop() -> None:
    assert check_exit(1000.0, 75.0, None) is None


def test_stop_can_only_tighten() -> None:
    assert tighten_stop(75.0, 80.0) == 80.0
    assert tighten_stop(75.0, 75.0) == 75.0
    with pytest.raises(StopWideningError):
        tighten_stop(80.0, 70.0)


def test_r_multiple() -> None:
    assert r_multiple(100, 150, 25) == 2.0
    assert r_multiple(100, 75, 25) == -1.0
    assert r_multiple(100, 110, 0) is None


# --------------------------------------------------------------------------- #
# Trailing
# --------------------------------------------------------------------------- #


def mgr(mode: str, activation: float, step: float, opt: OptionType = OptionType.CE) -> TrailingStopManager:
    return TrailingStopManager(TrailingConfig(enabled=True, mode=mode, activation=activation, step=step),  # type: ignore[arg-type]
                               opt)


def test_r_multiple_trailing_activates_and_locks_breakeven() -> None:
    m = mgr("R_MULTIPLE", activation=1.0, step=0.5)
    s = m.new_state(entry=100, stop=75)  # R = 25
    assert not m.update(s, 120)  # +0.8R: not active
    assert s.stop == 75 and not s.active
    assert m.update(s, 125)  # +1R: active; peak 125 - 12.5 = 112.5
    assert s.active and s.stop == 112.5
    assert m.update(s, 140) and s.stop == 127.5
    assert not m.update(s, 130)  # price falls: stop never loosens
    assert s.stop == 127.5


def test_r_multiple_never_below_breakeven() -> None:
    m = mgr("R_MULTIPLE", activation=1.0, step=2.0)
    s = m.new_state(entry=100, stop=75)
    m.update(s, 130)  # peak - 2R = 80 < entry -> breakeven
    assert s.stop == 100


def test_premium_percent_trailing() -> None:
    m = mgr("PREMIUM_PERCENT", activation=15, step=5)
    s = m.new_state(entry=100, stop=80)
    assert not m.update(s, 114)
    assert m.update(s, 115)  # 115 * 0.95 = 109.25
    assert s.stop == 109.25
    m.update(s, 200)
    assert s.stop == 190.0


def test_points_trailing() -> None:
    m = mgr("POINTS", activation=10, step=8)
    s = m.new_state(entry=100, stop=80)
    assert not m.update(s, 109.95)
    assert m.update(s, 110) and s.stop == 102.0


def test_trailing_stop_stays_below_market() -> None:
    m = mgr("POINTS", activation=1, step=0.01)
    s = m.new_state(entry=100, stop=80)
    m.update(s, 110)
    assert s.stop < 110


def test_trailing_disabled() -> None:
    m = TrailingStopManager(TrailingConfig(enabled=False, mode="POINTS", activation=1, step=1), OptionType.CE)
    s = m.new_state(100, 80)
    assert not m.update(s, 200) and s.stop == 80


def test_structural_trailing_ce_and_pe() -> None:
    ce = mgr("STRUCTURAL", activation=0, step=1, opt=OptionType.CE)
    s = ce.new_state(100, 80)
    assert ce.update_structure(s, 24450)
    assert ce.update_structure(s, 24480)  # higher swing low: tighter
    assert not ce.update_structure(s, 24400)  # lower: refused
    assert not ce.structural_breached(s, 24500)
    assert ce.structural_breached(s, 24479)
    assert not ce.update(s, 150) and s.stop == 80  # premium stop unchanged, still enforced

    pe = mgr("STRUCTURAL", activation=0, step=1, opt=OptionType.PE)
    p = pe.new_state(100, 80)
    pe.update_structure(p, 24600)
    assert pe.update_structure(p, 24550) and not pe.update_structure(p, 24650)
    assert pe.structural_breached(p, 24551)
