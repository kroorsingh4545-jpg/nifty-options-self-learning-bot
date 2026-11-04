"""SYNTHETIC sample data for tests and demos — NOT real NIFTY prices.

Everything here is generated from a seeded random walk. Datasets built by
this module are labelled ``source="SYNTHETIC"`` and every backtest report on
them says so. Never use results on synthetic data to judge a strategy.

What it generates, per trading day (weekends and the configured NSE holidays
are skipped):

* NIFTY index 1-minute candles: an overnight gap, then a day regime
  (trend up, trend down, range-bound, high volatility) driving a
  random walk sampled at 6 points per minute (OHLC from those points).
* NIFTY futures (current month, expiring the last Tuesday of each month):
  index plus a small basis, with a U-shaped intraday volume profile.
* NIFTY weekly options (expiring each Tuesday, or the previous trading day
  when Tuesday is a holiday): CE and PE every 50 points around the day's
  range, priced with Black-Scholes from the index path at a per-day implied
  volatility. Volume and OI peak at the money.
* A matching instrument list in the Upstox shape. The contract size of every
  SYNTHETIC contract is ``SyntheticSpec.lot_units``, which the caller must
  pass (no default); the backtester reads it back from the instrument list
  like any other dataset.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from backtest.data import ONE_MINUTE, HistoricalDataset
from config.settings import MarketSession
from core.clock import IST
from core.models import Candle, Instrument

INDEX_KEY = "NSE_INDEX|Nifty 50"
SYNTH_TAG = "SYNTH"
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL")


@dataclass(frozen=True, kw_only=True)
class SyntheticSpec:
    #: Contract size written into the SYNTHETIC instrument list. Required, with
    #: no default: there is no lot-size constant anywhere in production code.
    lot_units: int
    start: date = date(2026, 9, 1)
    days: int = 10
    seed: int = 7
    start_level: float = 25_000.0
    strike_step: int = 50
    strike_margin_points: float = 400.0  # strikes cover the day's range +/- this
    minute_vol_pct: float = 0.035  # std-dev of a 1-minute return, %
    gap_vol_pct: float = 0.35
    iv_mean: float = 0.13
    iv_vol: float = 0.02
    rate: float = 0.065
    futures_basis_pct: float = 0.15
    futures_volume_per_minute: int = 6_000
    option_volume_per_minute: int = 40_000
    option_oi: float = 2_000_000.0


# --------------------------------------------------------------------------- #
# Black-Scholes
# --------------------------------------------------------------------------- #


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, years: float, vol: float, rate: float, call: bool) -> float:
    if years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    if call:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #


def trading_days(start: date, n: int, session: MarketSession) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if session.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
        if (d - start).days > 3 * n + 30:
            raise ValueError("could not find enough trading days")
    return out


def _previous_trading_day(d: date, session: MarketSession) -> date:
    while not session.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def weekly_expiry(d: date, session: MarketSession) -> date:
    """The Tuesday on/after ``d`` (previous trading day if it is a holiday)."""
    tue = d + timedelta(days=(1 - d.weekday()) % 7)
    exp = _previous_trading_day(tue, session)
    if exp < d:  # this week's expiry was moved before d: take next week's
        exp = _previous_trading_day(tue + timedelta(days=7), session)
    return exp


def monthly_expiry(year: int, month: int, session: MarketSession) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    tue = last - timedelta(days=(last.weekday() - 1) % 7)
    return _previous_trading_day(tue, session)


def _symbol_date(d: date) -> str:
    return d.strftime("%d %b %y").upper()


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


def _option(strike: float, kind: str, expiry: date, spec: SyntheticSpec) -> Instrument:
    return Instrument(instrument_key=f"NSE_FO|{SYNTH_TAG}{expiry:%y%m%d}{int(strike)}{kind}",
                      trading_symbol=f"NIFTY {int(strike)} {kind} {_symbol_date(expiry)} {SYNTH_TAG}",
                      segment="NSE_FO", underlying_key=INDEX_KEY, underlying_symbol="NIFTY", instrument_type=kind,
                      expiry=expiry, strike=float(strike), lot_size=spec.lot_units, tick_size=0.05,
                      freeze_quantity=spec.lot_units * 27, weekly=True)


def _future(expiry: date, spec: SyntheticSpec) -> Instrument:
    return Instrument(instrument_key=f"NSE_FO|{SYNTH_TAG}FUT{expiry:%y%m%d}",
                      trading_symbol=f"NIFTY FUT {_symbol_date(expiry)} {SYNTH_TAG}", segment="NSE_FO",
                      underlying_key=INDEX_KEY, underlying_symbol="NIFTY", instrument_type="FUT", expiry=expiry,
                      strike=None, lot_size=spec.lot_units, tick_size=0.10, freeze_quantity=spec.lot_units * 27,
                      weekly=False)


def _round(price: float, tick: float) -> float:
    return round(max(tick, round(price / tick) * tick), 2)


def _day_path(rng: random.Random, open_level: float, regime: str, spec: SyntheticSpec,
              minutes: int) -> list[tuple[float, float, float, float]]:
    """Index (open, high, low, close) per minute."""
    vol = spec.minute_vol_pct / 100 * (1.8 if regime == "HIGH_VOL" else 1.0)
    drift = {"TREND_UP": 0.55, "TREND_DOWN": -0.55}.get(regime, 0.0) / 100 / minutes
    level, anchor = open_level, open_level
    out = []
    for _ in range(minutes):
        pts = [level]
        for _ in range(5):
            step = drift / 5 + rng.gauss(0.0, vol / math.sqrt(5))
            if regime == "RANGE":
                step -= 0.02 * (level - anchor) / anchor
            level *= 1 + step
            pts.append(level)
        out.append((pts[0], max(pts), min(pts), pts[-1]))
    return out


def _volume_profile(i: int, minutes: int) -> float:
    x = i / max(1, minutes - 1)
    return 0.6 + 1.6 * (x - 0.5) ** 2 * 4 / 2  # U-shape: ~1.4 at open/close, 0.6 midday


def generate_synthetic_dataset(spec: SyntheticSpec, session: MarketSession | None = None) -> HistoricalDataset:
    """A deterministic SYNTHETIC dataset (same spec -> same candles)."""
    session = session or MarketSession()
    rng = random.Random(spec.seed)  # noqa: S311 - synthetic prices, not cryptography
    days = trading_days(spec.start, spec.days, session)
    open_dt = datetime.combine(days[0], session.market_open)
    close_dt = datetime.combine(days[0], session.market_close)
    minutes = int((close_dt - open_dt).total_seconds() // 60)

    candles: dict[str, list[Candle]] = {}
    instruments: dict[str, Instrument] = {}
    futures: dict[date, Instrument] = {}
    level = spec.start_level

    def add(inst: Instrument | None, key: str, start: datetime, o: float, h: float, low: float, c: float,
            vol: int, oi: float | None) -> None:
        if inst is not None:
            instruments.setdefault(inst.instrument_key, inst)
        candles.setdefault(key, []).append(Candle(instrument_key=key, timeframe_minutes=1, start=start,
                                                  end=start + ONE_MINUTE, open=o, high=h, low=low, close=c,
                                                  volume=vol, oi=oi))

    for d in days:
        regime = rng.choice(REGIMES)
        level *= 1 + rng.gauss(0.0, spec.gap_vol_pct / 100)
        path = _day_path(rng, level, regime, spec, minutes)
        level = path[-1][3]
        iv = max(0.06, rng.gauss(spec.iv_mean, spec.iv_vol)) * (1.3 if regime == "HIGH_VOL" else 1.0)
        start0 = datetime.combine(d, session.market_open, tzinfo=IST)

        # futures: current month (rolls the day after expiry)
        fexp = monthly_expiry(d.year, d.month, session)
        if fexp < d:
            nm = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            fexp = monthly_expiry(nm.year, nm.month, session)
        fut = futures.setdefault(fexp, _future(fexp, spec))
        nxt = date(fexp.year + (fexp.month == 12), fexp.month % 12 + 1, 1)
        nfut = futures.setdefault(monthly_expiry(nxt.year, nxt.month, session),
                                  _future(monthly_expiry(nxt.year, nxt.month, session), spec))
        instruments.setdefault(nfut.instrument_key, nfut)
        basis = 1 + spec.futures_basis_pct / 100 * max(0.1, (fexp - d).days / 30)

        # weekly options for this day's expiry
        oexp = weekly_expiry(d, session)
        expiry_close = datetime.combine(oexp, session.market_close, tzinfo=IST)
        lo = min(p[2] for p in path) - spec.strike_margin_points
        hi = max(p[1] for p in path) + spec.strike_margin_points
        strikes = range(int(lo // spec.strike_step) * spec.strike_step,
                        int(hi // spec.strike_step + 1) * spec.strike_step + 1, spec.strike_step)

        for i, (o, h, low, c) in enumerate(path):
            t = start0 + timedelta(minutes=i)
            add(None, INDEX_KEY, t, round(o, 2), round(h, 2), round(low, 2), round(c, 2), 0, None)
            fv = int(spec.futures_volume_per_minute * _volume_profile(i, minutes) *
                     math.exp(rng.gauss(0.0, 0.35)) * (1 + 40 * abs(c - o) / o * 100))
            fo, fh, fl, fc = (_round(x * basis, fut.tick_size) for x in (o, h, low, c))
            add(fut, fut.instrument_key, t, fo, max(fo, fh, fl, fc), min(fo, fh, fl, fc), fc, fv, None)
            years = max((expiry_close - (t + ONE_MINUTE)).total_seconds(), 60.0) / (365 * 24 * 3600)
            for k in strikes:
                money = abs(c - k)
                weight = math.exp(-((money / 250.0) ** 2))
                for kind, call in (("CE", True), ("PE", False)):
                    inst = _option(k, kind, oexp, spec)
                    prices = [_round(black_scholes(x, k, years, iv, spec.rate, call), 0.05) for x in (o, h, low, c)]
                    po, ph, pl, pc = prices if call else (prices[0], prices[2], prices[1], prices[3])
                    ph, pl = max(po, ph, pl, pc), min(po, ph, pl, pc)
                    vol = int(spec.option_volume_per_minute * (0.05 + weight) * _volume_profile(i, minutes) *
                              math.exp(rng.gauss(0.0, 0.3)))
                    add(inst, inst.instrument_key, t, po, ph, pl, pc, vol, round(spec.option_oi * (0.1 + weight)))

    desc = (f"SYNTHETIC random-walk data (seed {spec.seed}, {len(days)} days from {days[0]}); NOT real NIFTY prices")
    return HistoricalDataset(tuple(instruments.values()), {k: tuple(v) for k, v in candles.items()},
                             source="SYNTHETIC", description=desc)


__all__ = ["INDEX_KEY", "SyntheticSpec", "black_scholes", "generate_synthetic_dataset", "monthly_expiry",
           "trading_days", "weekly_expiry"]
