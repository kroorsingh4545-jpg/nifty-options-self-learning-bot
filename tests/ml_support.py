"""Shared SYNTHETIC data for the ML tests (cached per test session).

Everything here is a seeded random walk (``backtest.synthetic_data``):
NOT real NIFTY prices. Models trained on it are SYNTHETIC research artefacts
and can never be promoted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

from backtest.data import HistoricalDataset
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from config.settings import BotConfig
from core.clock import IST
from core.models import Candle

#: Contract size of the SYNTHETIC instruments (tests only; production reads the instrument list).
SYNTH_LOT_UNITS = 65


@lru_cache(maxsize=4)
def synthetic_dataset(days: int = 12, seed: int = 7) -> HistoricalDataset:
    return generate_synthetic_dataset(SyntheticSpec(lot_units=SYNTH_LOT_UNITS, days=days, seed=seed,
                                                    strike_margin_points=200))


@lru_cache(maxsize=1)
def config() -> BotConfig:
    return load_config()


def bars_from_prices(key: str, day: date, start: tuple[int, int], ohlc: list[tuple[float, float, float, float]],
                     volume: int = 1000) -> list[Candle]:
    """Hand-made 1-minute candles (tests only)."""
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=IST)
    return [Candle(instrument_key=key, timeframe_minutes=1, start=t0 + timedelta(minutes=i),
                   end=t0 + timedelta(minutes=i + 1), open=o, high=h, low=lo, close=c, volume=volume)
            for i, (o, h, lo, c) in enumerate(ohlc)]
