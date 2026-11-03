"""Option chain, contract cache, and liquidity filter for NIFTY options."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from broker.broker_interface import MarketDataSource
from config.settings import LiquidityFilter as LiquidityConfig
from core.clock import now_ist
from core.enums import OptionType
from core.models import Greeks, Instrument

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class OptionQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_key: str
    option_type: OptionType
    strike: float
    expiry: date
    ltp: float
    close_price: float | None = None
    volume: int = 0
    oi: float = 0.0
    prev_oi: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    iv: float | None = None
    pop: float | None = None
    greeks: Greeks | None = None

    @property
    def premium(self) -> float:
        return self.ltp

    @property
    def spread(self) -> float | None:
        if not self.bid or not self.ask or self.ask < self.bid:
            return None
        return round(self.ask - self.bid, 4)

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.spread is not None else None  # type: ignore[operator]

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread as % of mid price."""
        mid = self.mid
        return 100 * self.spread / mid if mid and self.spread is not None else None

    @property
    def oi_change(self) -> float | None:
        return self.oi - self.prev_oi if self.prev_oi is not None else None


def _quote(side: dict[str, Any], opt: OptionType, strike: float, expiry: date) -> OptionQuote | None:
    if not side or not side.get("instrument_key"):
        return None
    md = side.get("market_data") or {}
    g = side.get("option_greeks") or {}
    return OptionQuote(
        instrument_key=side["instrument_key"],
        option_type=opt,
        strike=strike,
        expiry=expiry,
        ltp=float(md.get("ltp") or 0.0),
        close_price=md.get("close_price"),
        volume=int(md.get("volume") or 0),
        oi=float(md.get("oi") or 0.0),
        prev_oi=md.get("prev_oi"),
        bid=md.get("bid_price") or None,
        ask=md.get("ask_price") or None,
        bid_qty=md.get("bid_qty"),
        ask_qty=md.get("ask_qty"),
        iv=g.get("iv"),
        pop=g.get("pop"),
        greeks=Greeks(delta=g.get("delta"), gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"))
        if g
        else None,
    )


@dataclass
class OptionChain:
    underlying_key: str
    expiry: date
    spot: float
    quotes: list[OptionQuote]
    fetched_at: datetime = field(default_factory=now_ist)

    def strikes(self) -> list[float]:
        return sorted({q.strike for q in self.quotes})

    def get(self, strike: float, option_type: OptionType) -> OptionQuote | None:
        for q in self.quotes:
            if q.strike == strike and q.option_type is option_type:
                return q
        return None

    def atm_strike(self, step: int = 50) -> float:
        strikes = self.strikes()
        if not strikes:
            raise ValueError("empty option chain")
        target = round(self.spot / step) * step
        return min(strikes, key=lambda s: (abs(s - target), s))

    def select_strike(self, option_type: OptionType, mode: str = "ATM", offset_steps: int = 0, step: int = 50) -> float:
        """ATM, or ``offset_steps`` strikes ITM/OTM. ITM for CE is a *lower*
        strike; ITM for PE is a *higher* strike."""
        atm = self.atm_strike(step)
        if mode == "ATM" or offset_steps == 0:
            return atm
        direction = -1 if (mode == "ITM") == (option_type is OptionType.CE) else 1
        target = atm + direction * offset_steps * step
        if target not in self.strikes():
            raise ValueError(f"strike {target} not in chain")
        return target


def parse_option_chain(raw: dict[str, Any]) -> OptionChain:
    """Parse ``UpstoxRestClient.get_option_chain`` output."""
    rows = raw.get("rows") or []
    expiry = date.fromisoformat(raw["expiry"])
    quotes: list[OptionQuote] = []
    spot = 0.0
    for row in rows:
        strike = float(row["strike_price"])
        row_expiry = date.fromisoformat(row.get("expiry") or raw["expiry"])
        spot = float(row.get("underlying_spot_price") or spot)
        for side_key, opt in (("call_options", OptionType.CE), ("put_options", OptionType.PE)):
            q = _quote(row.get(side_key) or {}, opt, strike, row_expiry)
            if q is not None:
                quotes.append(q)
    return OptionChain(underlying_key=raw.get("underlying_key", NIFTY_INDEX_KEY), expiry=expiry, spot=spot,
                       quotes=quotes)


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiquidityResult:
    passed: bool
    reasons: tuple[str, ...]


class LiquidityFilter:
    """Reject contracts that are illiquid or have unusable quotes."""

    def __init__(self, cfg: LiquidityConfig, *, min_premium: float = 0.0, max_premium: float | None = None) -> None:
        self.cfg = cfg
        self.min_premium = min_premium
        self.max_premium = max_premium

    def check(self, q: OptionQuote) -> LiquidityResult:
        reasons: list[str] = []
        if q.volume < self.cfg.min_volume:
            reasons.append(f"volume {q.volume} < {self.cfg.min_volume}")
        if q.oi < self.cfg.min_oi:
            reasons.append(f"oi {q.oi:.0f} < {self.cfg.min_oi}")
        if q.spread_pct is None:
            reasons.append("no valid two-sided quote")
        elif q.spread_pct > self.cfg.max_spread_pct:
            reasons.append(f"spread {q.spread_pct:.2f}% > {self.cfg.max_spread_pct}%")
        if q.ltp <= 0:
            reasons.append("no last traded price")
        elif q.ltp < self.min_premium:
            reasons.append(f"premium {q.ltp} < {self.min_premium}")
        elif self.max_premium is not None and q.ltp > self.max_premium:
            reasons.append(f"premium {q.ltp} > {self.max_premium}")
        return LiquidityResult(passed=not reasons, reasons=tuple(reasons))

    def filter(self, quotes: Iterable[OptionQuote]) -> list[OptionQuote]:
        return [q for q in quotes if self.check(q).passed]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


class OptionChainFetcher:
    """Fetches contracts (cached per day) and option chains via REST."""

    def __init__(
        self,
        source: MarketDataSource,
        underlying_key: str = NIFTY_INDEX_KEY,
        *,
        today_fn: Callable[[], date] = lambda: now_ist().date(),
    ) -> None:
        self.source = source
        self.underlying_key = underlying_key
        self._today = today_fn
        self._contracts: list[Instrument] | None = None
        self._contracts_day: date | None = None

    async def contracts(self, *, refresh: bool = False) -> list[Instrument]:
        today = self._today()
        if refresh or self._contracts is None or self._contracts_day != today:
            self._contracts = await self.source.get_option_contracts(self.underlying_key)
            self._contracts_day = today
        return self._contracts

    async def expiries(self) -> list[date]:
        today = self._today()
        return sorted({c.expiry for c in await self.contracts() if c.expiry is not None and c.expiry >= today})

    async def nearest_expiry(self) -> date:
        exp = await self.expiries()
        if not exp:
            raise LookupError("no upcoming expiries for underlying")
        return exp[0]

    async def lot_size(self, expiry: date | None = None) -> int:
        target = expiry or await self.nearest_expiry()
        sizes = {c.lot_size for c in await self.contracts() if c.expiry == target}
        if len(sizes) != 1:
            raise LookupError(f"ambiguous or missing lot size for {target}: {sorted(sizes)}")
        return sizes.pop()

    async def fetch(self, expiry: date | None = None) -> OptionChain:
        target = expiry or await self.nearest_expiry()
        return parse_option_chain(await self.source.get_option_chain(self.underlying_key, target))
