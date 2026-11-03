"""Indian options transaction charges."""

from __future__ import annotations

import pytest

from broker.charges import ChargeCalculator
from config.settings import CostsSection
from core.enums import OrderSide

CALC = ChargeCalculator(CostsSection())


def test_buy_side_components() -> None:
    # 75 qty @ ₹100 -> turnover ₹7,500
    c = CALC.compute(OrderSide.BUY, 75, 100.0)
    assert c.brokerage == 20.0
    assert c.stt == 0.0  # no STT on option buy
    assert c.exchange_txn == pytest.approx(2.63, abs=0.01)  # 0.03503%
    assert c.sebi_fee == pytest.approx(0.01, abs=0.01)
    assert c.stamp_duty == pytest.approx(0.23, abs=0.01)  # 0.003% buy side
    assert c.gst == pytest.approx((20 + 2.627 + 0.0075) * 0.18, abs=0.01)
    assert c.total == pytest.approx(c.brokerage + c.exchange_txn + c.sebi_fee + c.stamp_duty + c.gst, abs=0.011)


def test_sell_side_has_stt_and_no_stamp() -> None:
    c = CALC.compute(OrderSide.SELL, 75, 100.0)
    assert c.stt == pytest.approx(11.25)  # 0.15% of 7,500
    assert c.stamp_duty == 0.0


def test_brokerage_once_per_order() -> None:
    c = CALC.compute(OrderSide.BUY, 75, 100.0, include_brokerage=False)
    assert c.brokerage == 0 and c.gst < 1


def test_round_trip() -> None:
    rt = CALC.round_trip(75, 100.0, 120.0)
    assert rt == pytest.approx(CALC.compute(OrderSide.BUY, 75, 100).total + CALC.compute(OrderSide.SELL, 75, 120).total)
    # buy 7,500: 20 + 2.63 + 0.01 + 0.23 + GST 4.07 = 26.94
    # sell 9,000: 20 + STT 13.50 + 3.15 + 0.01 + GST 4.17 = 40.83
    assert rt == pytest.approx(67.77, abs=0.02)


def test_rates_are_configurable() -> None:
    zero = ChargeCalculator(CostsSection(brokerage_per_order=0, stt_sell_pct=0, exchange_txn_pct=0,
                                         sebi_fee_per_crore=0, stamp_duty_buy_pct=0, gst_pct=0))
    assert zero.compute(OrderSide.SELL, 75, 100).total == 0


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        CALC.compute(OrderSide.BUY, 0, 100)
