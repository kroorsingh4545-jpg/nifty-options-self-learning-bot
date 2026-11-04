"""Strategy id -> implementation. Shared by the runtime (live / PAPER /
backtest) and the ML layer (offline signal replay), so both build the same
strategy code for a configured strategy id."""

from __future__ import annotations

from strategies.base_strategy import BaseStrategy
from strategies.orb_options import OrbOptionsStrategy
from strategies.vwap_ema import VwapEmaStrategy

STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "orb_options": OrbOptionsStrategy,
    "vwap_ema": VwapEmaStrategy,
}

__all__ = ["STRATEGY_CLASSES"]
