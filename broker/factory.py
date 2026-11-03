"""Broker wiring (dependency injection).

This is check (a) of the two independent PAPER/LIVE guards: in PAPER mode the
only broker that can be constructed is ``PaperBroker``. Check (b) lives in
``UpstoxRestClient``, whose order methods refuse unless LIVE is enabled.
"""

from __future__ import annotations

from collections.abc import Iterable

from broker.broker_interface import Broker
from broker.errors import LiveTradingDisabledError
from broker.paper_broker import PaperBroker, QuoteLookup
from config import hard_limits
from config.settings import BotConfig
from core.enums import TradingMode
from core.models import Instrument


def create_broker(cfg: BotConfig, *, quotes: QuoteLookup, instruments: Iterable[Instrument] = ()) -> Broker:
    if cfg.mode is TradingMode.PAPER:
        return PaperBroker(
            quotes=quotes,
            settings=cfg.settings.paper_broker,
            costs=cfg.settings.costs,
            instruments=instruments,
        )
    if not hard_limits.LIVE_TRADING_AVAILABLE:
        raise LiveTradingDisabledError("LIVE broker is not available in this build")
    raise LiveTradingDisabledError("LIVE broker is not implemented")  # pragma: no cover
