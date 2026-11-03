"""Identifier generation with explicit PAPER / LIVE separation.

Every locally generated order or trade identifier is prefixed with the trading
mode (``PAPER-`` or ``LIVE-``). The SQLite schema enforces the same rule, so a
PAPER record can never be mistaken for a LIVE one.
"""

from __future__ import annotations

import re
import uuid

from core.enums import TradingMode

STRATEGY_ID_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
_STRATEGY_ID_RE = re.compile(STRATEGY_ID_PATTERN)


def _token(length: int = 12) -> str:
    return uuid.uuid4().hex[:length]


def _check_strategy_id(strategy_id: str) -> None:
    if not _STRATEGY_ID_RE.match(strategy_id):
        raise ValueError(f"invalid strategy_id: {strategy_id!r}")


def new_client_order_id(mode: TradingMode, strategy_id: str) -> str:
    """Unique, mode-prefixed client order id (idempotency key)."""
    _check_strategy_id(strategy_id)
    return f"{TradingMode(mode).value}-{strategy_id}-{_token()}"


def new_trade_uid(mode: TradingMode, strategy_id: str) -> str:
    _check_strategy_id(strategy_id)
    return f"{TradingMode(mode).value}-T-{strategy_id}-{_token()}"


def new_paper_order_id() -> str:
    """Simulated order id issued by the paper broker (never an Upstox id)."""
    return f"PO-{_token(16)}"


def new_paper_fill_id() -> str:
    return f"PF-{_token(16)}"


def new_uid(prefix: str) -> str:
    return f"{prefix}-{_token(16)}"


def mode_of(identifier: str) -> TradingMode:
    """Return the mode encoded in a mode-prefixed identifier."""
    for mode in TradingMode:
        if identifier.startswith(f"{mode.value}-"):
            return mode
    raise ValueError(f"identifier has no PAPER-/LIVE- prefix: {identifier!r}")
