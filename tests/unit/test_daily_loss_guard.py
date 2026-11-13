"""Daily loss limits, trade caps, cooldowns, consecutive-loss pause, persistence."""

from __future__ import annotations

from datetime import datetime

import pytest

from config.loader import load_config
from core.clock import IST
from core.enums import TradingMode
from data.storage.repositories import Repositories
from risk.daily_loss_guard import DailyLossGuard
from tests.fakes import FakeWallClock

CFG = load_config(use_default_env_file=False)
LIMITS = {sid: s.risk for sid, s in CFG.strategies.strategies.items()}


class Rec:
    def __init__(self) -> None:
        self.strategy: list[tuple[str, float]] = []
        self.global_: list[float] = []
        self.paused: list[tuple[str, str]] = []


def make(wall: FakeWallClock | None = None, repos: Repositories | None = None) -> tuple[DailyLossGuard, Rec]:
    r = Rec()
    g = DailyLossGuard(CFG.risk, LIMITS, mode=TradingMode.PAPER, repo=repos.daily_pnl if repos else None,
                       now_fn=wall or FakeWallClock(datetime(2026, 11, 3, 10, 0, tzinfo=IST)),
                       on_strategy_breach=lambda s, loss: r.strategy.append((s, loss)),
                       on_global_breach=r.global_.append, on_pause=lambda s, why: r.paused.append((s, why)))
    return g, r


def test_strategy_limit_breach_includes_unrealized() -> None:
    g, r = make()
    g.record_trade_close("orb_options", -3000)
    assert g.can_enter("vwap_ema")[0]
    g.update_unrealized("orb_options", -1999)
    assert r.strategy == []
    g.update_unrealized("orb_options", -2000)  # total loss 5,000
    assert r.strategy == [("orb_options", 5000)]
    ok, why = g.can_enter("orb_options")
    assert not ok and any("daily loss" in w for w in why)
    assert g.can_enter("vwap_ema")[0]  # isolation: other strategy unaffected


def test_breach_callback_fires_once() -> None:
    g, r = make()
    g.update_unrealized("orb_options", -6000)
    g.update_unrealized("orb_options", -7000)
    assert len(r.strategy) == 1


def test_global_limit_breach() -> None:
    g, r = make()
    g.update_unrealized("orb_options", -4999)
    g.update_unrealized("vwap_ema", -4999)
    assert r.global_ == []
    g.record_trade_close("vwap_ema", -2)  # global loss 10,000
    assert r.global_ == [pytest.approx(10000)]
    assert not g.can_enter("orb_options")[0]


def test_profits_offset_losses() -> None:
    g, r = make()
    g.record_trade_close("orb_options", 4000)
    g.record_trade_close("orb_options", -8000)
    assert r.strategy == []  # net -4,000 < 5,000


def test_trade_cap() -> None:
    g, _ = make()
    for _ in range(4):
        g.record_entry("orb_options")
    ok, why = g.can_enter("orb_options")
    assert not ok and "trade cap" in why[0]


def test_cooldown_after_loss() -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 10, 0, tzinfo=IST))
    g, _ = make(wall)
    g.record_trade_close("orb_options", -500)
    ok, why = g.can_enter("orb_options")
    assert not ok and "cooldown" in why[0] and "15 min" in why[0]
    wall.advance(minutes=14, seconds=59)
    assert not g.can_enter("orb_options")[0]
    wall.advance(seconds=1)
    assert g.can_enter("orb_options")[0]


def test_win_does_not_start_cooldown() -> None:
    g, _ = make()
    g.record_trade_close("orb_options", 500)
    assert g.can_enter("orb_options")[0]


def test_consecutive_loss_pause_and_reset_on_win() -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 10, 0, tzinfo=IST))
    g, r = make(wall)
    g.record_trade_close("orb_options", -100)
    g.record_trade_close("orb_options", 50)  # win resets the streak
    g.record_trade_close("orb_options", -100)
    g.record_trade_close("orb_options", -100)
    assert r.paused == []
    g.record_trade_close("orb_options", -100)  # 3rd consecutive
    assert r.paused and r.paused[0][0] == "orb_options"
    wall.advance(hours=2)  # cooldown long over, still paused for the day
    ok, why = g.can_enter("orb_options")
    assert not ok and any("paused" in w for w in why)


def test_remaining_risk_budget_caps_per_trade_risk() -> None:
    g, _ = make()
    assert g.remaining_risk_budget("orb_options") == 3000  # PAPER per-trade risk (owner decision 2026-11-13)
    g.record_trade_close("orb_options", -3500)
    assert g.remaining_risk_budget("orb_options") == pytest.approx(1500)
    g.update_unrealized("vwap_ema", -4000)
    g.record_trade_close("orb_options", 0)
    # global left = 10,000 - 7,500 = 2,500 ; orb strategy left = 1,500
    assert g.remaining_risk_budget("orb_options") == pytest.approx(1500)
    g.update_unrealized("vwap_ema", -4900)
    assert g.remaining_risk_budget("vwap_ema") == pytest.approx(100)


def test_new_day_resets() -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 15, 0, tzinfo=IST))
    g, _ = make(wall)
    g.update_unrealized("orb_options", -6000)
    assert not g.can_enter("orb_options")[0]
    wall.now = datetime(2026, 11, 4, 9, 20, tzinfo=IST)
    assert g.can_enter("orb_options")[0]


def test_persistence_and_restore(paper: Repositories) -> None:
    wall = FakeWallClock(datetime(2026, 11, 3, 11, 0, tzinfo=IST))
    g, _ = make(wall, paper)
    g.record_entry("orb_options")
    g.record_trade_close("orb_options", -6000)
    rec = paper.daily_pnl.get(wall.now.date(), "orb_options")
    assert rec is not None and rec.loss_limit_breached and rec.realized_pnl == -6000

    g2, _ = make(wall, paper)  # simulated restart
    g2.restore()
    assert g2.state["orb_options"].breached
    assert not g2.can_enter("orb_options")[0]
    assert g2.can_enter("vwap_ema")[0]


def test_unknown_strategy() -> None:
    g, _ = make()
    with pytest.raises(KeyError):
        g.record_entry("ghost")
