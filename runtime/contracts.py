"""Option contract selection and the subscribed strike universe."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date

from config.settings import StrategyConfig
from core.enums import OptionType
from core.interfaces import ContractChoice
from core.models import Instrument, Signal, Tick
from data.option_chain import LiquidityFilter, OptionQuote


def nearest_expiry(contracts: Iterable[Instrument], today: date) -> date | None:
    exps = sorted({c.expiry for c in contracts if c.expiry is not None and c.expiry >= today})
    return exps[0] if exps else None


class OptionUniverse:
    """The option contracts (nearest expiry, ATM +/- ``band`` strikes, CE and
    PE) kept subscribed on the market feed so a fresh quote is always
    available when a signal arrives."""

    def __init__(self, contracts: Sequence[Instrument], *, expiry: date, band: int, step: int = 50) -> None:
        self.by_key = {c.instrument_key: c for c in contracts if c.expiry == expiry and c.option_type}
        self.by_strike: dict[tuple[float, OptionType], Instrument] = {}
        for c in self.by_key.values():
            assert c.strike is not None and c.option_type is not None
            self.by_strike[(c.strike, c.option_type)] = c
        self.expiry = expiry
        self.band = band
        self.step = step
        self.subscribed: set[str] = set()
        self.center: float | None = None

    def strikes(self) -> list[float]:
        return sorted({s for s, _ in self.by_strike})

    def atm(self, spot: float) -> float:
        strikes = self.strikes()
        if not strikes:
            raise LookupError("no strikes for expiry")
        target = round(spot / self.step) * self.step
        return min(strikes, key=lambda s: (abs(s - target), s))

    def wanted(self, spot: float) -> set[str]:
        atm = self.atm(spot)
        keys: set[str] = set()
        for i in range(-self.band, self.band + 1):
            for opt in (OptionType.CE, OptionType.PE):
                inst = self.by_strike.get((atm + i * self.step, opt))
                if inst is not None:
                    keys.add(inst.instrument_key)
        return keys

    def recenter(self, spot: float, keep: Iterable[str] = ()) -> tuple[set[str], set[str]]:
        """Returns (to_subscribe, to_unsubscribe). Held contracts in ``keep``
        are never unsubscribed (their stops depend on live ticks)."""
        want = self.wanted(spot) | set(keep)
        add, remove = want - self.subscribed, self.subscribed - want
        self.subscribed = want
        self.center = self.atm(spot)
        return add, remove

    def select(self, spot: float, opt: OptionType, mode: str, offset: int) -> Instrument | None:
        atm = self.atm(spot)
        if mode == "ATM" or offset == 0:
            strike = atm
        else:
            direction = -1 if (mode == "ITM") == (opt is OptionType.CE) else 1
            strike = atm + direction * offset * self.step
        return self.by_strike.get((strike, opt))


def quote_from_tick(inst: Instrument, tick: Tick) -> OptionQuote:
    assert inst.strike is not None and inst.expiry is not None and inst.option_type is not None
    return OptionQuote(instrument_key=inst.instrument_key, option_type=inst.option_type, strike=inst.strike,
                       expiry=inst.expiry, ltp=tick.ltp, volume=tick.volume_today or 0, oi=tick.oi or 0.0,
                       bid=tick.bid, ask=tick.ask, bid_qty=tick.bid_qty, ask_qty=tick.ask_qty, iv=tick.iv,
                       greeks=tick.greeks)


class OptionContractResolver:
    """Signal -> concrete option contract, using the strategy's strike rules
    and liquidity filter on live (WebSocket) quotes."""

    def __init__(
        self,
        *,
        universe: OptionUniverse,
        strategies: dict[str, StrategyConfig],
        spot_fn: Callable[[], float | None],
        quote_fn: Callable[[str], Tick | None],
    ) -> None:
        self.universe = universe
        self.strategies = strategies
        self._spot = spot_fn
        self._quote = quote_fn

    def resolve(self, signal: Signal) -> ContractChoice:
        scfg = self.strategies[signal.strategy_id]
        spot = self._spot()
        if spot is None:
            return ContractChoice(None, ("no NIFTY spot price",))
        ss = scfg.strike_selection
        inst = self.universe.select(spot, signal.option_type, ss.mode, ss.offset_steps)
        if inst is None:
            return ContractChoice(None, (f"no {ss.mode}{ss.offset_steps} {signal.option_type.value} contract",))
        tick = self._quote(inst.instrument_key)
        if tick is None:
            return ContractChoice(None, (f"no quote for {inst.trading_symbol}",))
        liq = LiquidityFilter(ss.liquidity, min_premium=ss.min_premium, max_premium=ss.max_premium)
        res = liq.check(quote_from_tick(inst, tick))
        if not res.passed:
            return ContractChoice(None, tuple(f"{inst.trading_symbol}: {r}" for r in res.reasons))
        return ContractChoice(inst)
