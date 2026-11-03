"""Mode-prefixed identifiers."""

from __future__ import annotations

import pytest

from core.enums import TradingMode
from core.ids import mode_of, new_client_order_id, new_paper_fill_id, new_paper_order_id, new_trade_uid


@pytest.mark.parametrize("mode", list(TradingMode))
def test_ids_carry_mode_prefix(mode: TradingMode) -> None:
    assert new_client_order_id(mode, "orb_options").startswith(f"{mode.value}-orb_options-")
    assert new_trade_uid(mode, "vwap_ema").startswith(f"{mode.value}-T-vwap_ema-")
    assert mode_of(new_client_order_id(mode, "orb_options")) is mode


def test_ids_are_unique() -> None:
    ids = {new_client_order_id(TradingMode.PAPER, "orb_options") for _ in range(2000)}
    assert len(ids) == 2000


def test_paper_broker_ids_are_distinct_from_local_ids() -> None:
    assert new_paper_order_id().startswith("PO-")
    assert new_paper_fill_id().startswith("PF-")


@pytest.mark.parametrize("bad", ["", "ORB", "1abc", "a", "has-dash", "x" * 40])
def test_invalid_strategy_id_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        new_client_order_id(TradingMode.PAPER, bad)


def test_mode_of_rejects_unprefixed() -> None:
    with pytest.raises(ValueError):
        mode_of("PO-123")
