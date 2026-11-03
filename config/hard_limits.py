"""Hard risk ceilings from the project specification.

YAML config may make any of these *stricter* but never looser. Values loaded
from ``risk_limits.yaml`` and ``strategy_defaults.yaml`` are validated against
these constants at startup; a config that weakens them refuses to load.

ML, RL, and the learning loop must treat this module as read-only.
"""

from __future__ import annotations

from datetime import time
from typing import Final

# LIVE order routing is not implemented. Until the LIVE preconditions in
# docs/safety-policy.md are implemented and verified, TRADING_MODE=LIVE is
# refused at config load.
LIVE_TRADING_AVAILABLE: Final = False

# Capital
TOTAL_CAPITAL: Final = 200_000.0
PER_STRATEGY_CAPITAL: Final = 100_000.0

# Per trade
MAX_RISK_PER_TRADE_RS: Final = 2_000.0
MAX_RISK_PER_TRADE_PCT: Final = 0.02  # of strategy capital

# Per strategy, per day
MAX_DAILY_LOSS_PER_STRATEGY_RS: Final = 5_000.0
MAX_DAILY_LOSS_PER_STRATEGY_PCT: Final = 0.05
MAX_OPEN_TRADES_PER_STRATEGY: Final = 2
MAX_TRADES_PER_STRATEGY_PER_DAY: Final = 4
MIN_LOSS_COOLDOWN_MINUTES: Final = 15
MAX_CONSECUTIVE_LOSSES: Final = 3

# Global, per day
MAX_GLOBAL_DAILY_LOSS_RS: Final = 10_000.0
MAX_GLOBAL_DAILY_LOSS_PCT: Final = 0.05  # of total capital
MAX_TOTAL_OPEN_TRADES: Final = 4

# Session timing (IST)
LATEST_NEW_ENTRY_TIME: Final = time(14, 45)
LATEST_SQUARE_OFF_START: Final = time(15, 10)
LATEST_SQUARE_OFF_END: Final = time(15, 20)

# Kill-switch thresholds (config may only lower these)
MAX_WS_DISCONNECT_GRACE_SECONDS: Final = 60
MAX_ABNORMAL_SPREAD_PCT: Final = 10.0
MAX_ABNORMAL_SPREAD_TICKS: Final = 10
MAX_CONSECUTIVE_API_FAILURES: Final = 5
MAX_CONSECUTIVE_ORDER_REJECTIONS: Final = 5

# Reconciliation must run at least every 5 minutes.
MAX_RECONCILE_INTERVAL_SECONDS: Final = 300

# Upstox API rate limits (official, per project spec — re-verify before LIVE)
MIN_RATE_LIMIT_SAFETY_BUFFER: Final = 0.20
UPSTOX_OFFICIAL_LIMITS: Final = {
    "order_api": {"per_second": 10, "per_minute": 500, "per_30_minutes": 2_000},
    "standard_api": {"per_second": 50, "per_minute": 500, "per_30_minutes": 2_000},
}

# Actions that must all run on a global daily-loss breach.
REQUIRED_GLOBAL_BREACH_ACTIONS: Final = frozenset(
    {
        "CANCEL_PENDING_ORDERS",
        "EXIT_ALL_POSITIONS",
        "DISABLE_ALL_STRATEGIES",
        "TRIGGER_KILL_SWITCH",
        "SEND_ALERTS",
    }
)
