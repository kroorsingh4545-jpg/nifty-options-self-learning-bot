"""Position sizing."""

from __future__ import annotations

import pytest

from broker.charges import ChargeCalculator
from risk.position_sizer import size_position

CHARGES = ChargeCalculator()


def cost(q: int, e: float, x: float) -> float:
    return CHARGES.round_trip(q, e, x)


def test_risk_limited_whole_lots() -> None:
    r = size_position(entry_price=100, stop_price=80, lot_size=75, risk_budget=2000, available_margin=100_000)
    assert r.lots == 1 and r.quantity == 75  # 2 lots would risk 3,000
    assert r.risk_amount == pytest.approx(1500)
    assert r.limited_by == "risk"


def test_tighter_stop_allows_more_lots() -> None:
    r = size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=100_000)
    assert r.lots == 5 and r.risk_amount <= 2000


def test_charges_and_slippage_count_against_budget() -> None:
    # 5 lots risk 1,875 before costs; with slippage + charges it no longer fits.
    r = size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=100_000,
                      exit_slippage=0.05, cost_fn=cost)
    assert r.lots == 4
    assert r.risk_amount <= 2000
    assert r.risk_amount == pytest.approx(300 * 5.05 + cost(300, 100, 95), abs=0.01)


def test_margin_limited() -> None:
    r = size_position(entry_price=200, stop_price=199, lot_size=75, risk_budget=2000, available_margin=31_000)
    assert r.limited_by == "margin" and r.lots == 2  # 3 lots need 45,900


def test_freeze_quantity_cap() -> None:
    r = size_position(entry_price=10, stop_price=9.9, lot_size=75, risk_budget=2000, available_margin=1e7,
                      freeze_quantity=1800)
    assert r.quantity == 1800 and r.limited_by == "freeze"


def test_ml_multiplier_only_reduces() -> None:
    full = size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=1e6)
    half = size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=1e6,
                         size_multiplier=0.5)
    assert half.lots < full.lots
    with pytest.raises(ValueError, match="only reduce"):
        size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=1e6,
                      size_multiplier=1.5)
    with pytest.raises(ValueError):
        size_position(entry_price=100, stop_price=95, lot_size=75, risk_budget=2000, available_margin=1e6,
                      size_multiplier=0)


@pytest.mark.parametrize(("budget", "margin", "reason"), [(500, 1e6, "risk budget"), (2000, 1000, "margin")])
def test_zero_size_with_reason(budget: float, margin: float, reason: str) -> None:
    r = size_position(entry_price=100, stop_price=80, lot_size=75, risk_budget=budget, available_margin=margin)
    assert not r.ok and r.quantity == 0 and reason in (r.reason or "")


@pytest.mark.parametrize("stop", [100, 120, 0, -5])
def test_invalid_stop_gives_zero(stop: float) -> None:
    r = size_position(entry_price=100, stop_price=stop, lot_size=75, risk_budget=2000, available_margin=1e6)
    assert not r.ok


def test_never_exceeds_budget_property() -> None:
    for entry in (20.0, 55.5, 100.0, 250.0, 399.95):
        for pct in (0.1, 0.2, 0.3, 0.5):
            stop = round(entry * (1 - pct), 2)
            r = size_position(entry_price=entry, stop_price=stop, lot_size=75, risk_budget=2000,
                              available_margin=100_000, exit_slippage=0.05, cost_fn=cost)
            assert r.risk_amount <= 2000 + 1e-6
            assert r.capital_required <= 100_000
