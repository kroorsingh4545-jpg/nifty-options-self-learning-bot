"""Option chain parsing, strike selection, liquidity filter, fetcher."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from broker.broker_interface import MarketDataSource
from broker.upstox_mapping import parse_instrument
from config.settings import LiquidityFilter as LiquidityConfig
from core.enums import OptionType
from core.models import Candle, Instrument
from data.option_chain import LiquidityFilter, OptionChainFetcher, OptionQuote, parse_option_chain
from tests.fakes import chain_row, future_contract, option_contract

EXP = date(2026, 11, 10)


def chain(spot: float = 24512.0) -> Any:
    rows = [chain_row(s, spot) for s in range(24300, 24750, 50)]
    return parse_option_chain({"underlying_key": "NSE_INDEX|Nifty 50", "expiry": EXP.isoformat(), "rows": rows})


def test_parse_fields() -> None:
    c = chain()
    assert c.spot == 24512.0 and c.expiry == EXP
    q = c.get(24500, OptionType.CE)
    assert q is not None
    assert q.instrument_key == "NSE_FO|24500CE"
    assert q.ltp == pytest.approx(112.0)
    assert (q.bid, q.ask) == (111.5, 112.5)
    assert q.volume == 250000 and q.oi == 1200000
    assert q.spread == pytest.approx(1.0)
    assert q.spread_pct == pytest.approx(100 / 112, rel=1e-3)
    assert q.oi_change == 100000
    assert q.greeks is not None and q.iv == 13.5
    assert len(c.quotes) == 18


def test_atm_and_itm_otm_selection() -> None:
    c = chain(24512.0)
    assert c.atm_strike(50) == 24500
    assert c.select_strike(OptionType.CE, "ITM", 1) == 24450  # CE ITM = lower
    assert c.select_strike(OptionType.CE, "OTM", 2) == 24600
    assert c.select_strike(OptionType.PE, "ITM", 1) == 24550  # PE ITM = higher
    assert c.select_strike(OptionType.PE, "OTM", 1) == 24450
    with pytest.raises(ValueError):
        c.select_strike(OptionType.CE, "OTM", 10)


def test_atm_ties_round_to_nearest() -> None:
    assert chain(24526.0).atm_strike(50) == 24550
    assert chain(24524.0).atm_strike(50) == 24500


def q(**over: Any) -> OptionQuote:
    base: dict[str, Any] = dict(instrument_key="K", option_type=OptionType.CE, strike=24500, expiry=EXP, ltp=100,
                                volume=50000, oi=200000, bid=99.5, ask=100.5)
    base.update(over)
    return OptionQuote(**base)


LIQ = LiquidityFilter(LiquidityConfig(min_volume=10000, min_oi=50000, max_spread_pct=1.5), min_premium=40,
                      max_premium=400)


def test_liquid_contract_passes() -> None:
    assert LIQ.check(q()).passed


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"volume": 500}, "volume"),
        ({"oi": 1000}, "oi"),
        ({"bid": 95.0, "ask": 105.0}, "spread"),
        ({"bid": None}, "two-sided"),
        ({"bid": 101.0, "ask": 100.0}, "two-sided"),  # crossed quote
        ({"ltp": 0}, "last traded"),
        ({"ltp": 20, "bid": 19.9, "ask": 20.1}, "premium"),
        ({"ltp": 500, "bid": 499.5, "ask": 500.5}, "premium"),
    ],
)
def test_illiquid_contracts_rejected(over: dict[str, Any], reason: str) -> None:
    res = LIQ.check(q(**over))
    assert not res.passed
    assert any(reason in r for r in res.reasons)


def test_filter_list() -> None:
    assert len(LIQ.filter([q(), q(volume=1), q(oi=1)])) == 1


class Source(MarketDataSource):
    def __init__(self) -> None:
        self.contract_calls = 0
        self.chain_expiry: date | None = None

    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        raise AssertionError("contracts must come from the instrument list")

    async def get_instruments(self, underlying_symbols: Sequence[str] = ("NIFTY",)) -> list[Instrument]:
        self.contract_calls += 1
        return [parse_instrument(option_contract(24500, "CE", "2026-11-03", lot=65)),
                parse_instrument(option_contract(24500, "CE", "2026-11-10", lot=65)),
                parse_instrument(option_contract(24500, "PE", "2026-11-17", lot=65)),
                parse_instrument(option_contract(24500, "PE", "2026-10-27", lot=65)),  # expired
                parse_instrument(future_contract("2026-11-23", "NSE_FO|FUT1", lot=65))]  # not an option

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        self.chain_expiry = expiry
        return {"underlying_key": underlying_key, "expiry": expiry.isoformat(),
                "rows": [chain_row(24500, 24500, expiry.isoformat())]}

    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        return {}

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        return []


async def test_fetcher_expiry_lot_size_and_cache() -> None:
    today = {"d": date(2026, 11, 4)}
    src = Source()
    f = OptionChainFetcher(src, today_fn=lambda: today["d"])
    assert await f.nearest_expiry() == date(2026, 11, 10)
    assert await f.expiries() == [date(2026, 11, 10), date(2026, 11, 17)]
    assert await f.lot_size() == 65  # from the instrument list
    oc = await f.fetch()
    assert src.chain_expiry == date(2026, 11, 10) and oc.get(24500, OptionType.PE) is not None
    assert src.contract_calls == 1  # cached within the day
    today["d"] = date(2026, 11, 5)
    await f.contracts()
    assert src.contract_calls == 2  # refreshed on a new day
