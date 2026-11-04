"""Shared domain enumerations.

These values are mirrored by CHECK constraints in ``data/storage/schema.sql``;
``tests/unit/test_database.py`` verifies the two stay in sync.
"""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class AutonomyLevel(StrEnum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    PAPER_AUTO_IMPROVE = "PAPER_AUTO_IMPROVE"
    LIVE_CANDIDATE_APPROVAL = "LIVE_CANDIDATE_APPROVAL"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class ProductType(StrEnum):
    INTRADAY = "I"
    DELIVERY = "D"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    # Outcome not known (e.g. request timeout). Must be reconciled with the
    # broker before any retry.
    UNKNOWN = "UNKNOWN"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    SQUARE_OFF = "SQUARE_OFF"
    KILL_SWITCH = "KILL_SWITCH"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class StrategyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    KILLED = "KILLED"


class RiskSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SnapshotPurpose(StrEnum):
    SIGNAL = "SIGNAL"
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class ModelType(StrEnum):
    SIGNAL_FILTER = "SIGNAL_FILTER"
    REGIME_CLASSIFIER = "REGIME_CLASSIFIER"
    OUTCOME_PREDICTOR = "OUTCOME_PREDICTOR"
    RL_PPO = "RL_PPO"


class ModelRunStatus(StrEnum):
    TRAINED = "TRAINED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    PAPER_READY = "PAPER_READY"
    LIVE_CANDIDATE = "LIVE_CANDIDATE"
    # Only reachable with a recorded human approver (enforced in the schema).
    LIVE_APPROVED = "LIVE_APPROVED"
    ARCHIVED = "ARCHIVED"


class LearningStage(StrEnum):
    LOG = "LOG"
    REFLECT = "REFLECT"
    HYPOTHESIZE = "HYPOTHESIZE"
    MUTATE = "MUTATE"
    VERIFY = "VERIFY"
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"


class ControlCommandType(StrEnum):
    """Operator commands the dashboard may queue (executed by the bot)."""

    KILL_SWITCH = "KILL_SWITCH"
    RESET_KILL_SWITCH = "RESET_KILL_SWITCH"
    EXIT_POSITION = "EXIT_POSITION"
    ENGINE_START = "ENGINE_START"
    ENGINE_PAUSE = "ENGINE_PAUSE"
    ENGINE_STOP = "ENGINE_STOP"
    SET_AUTONOMY = "SET_AUTONOMY"
    REQUEST_LIVE = "REQUEST_LIVE"
    REQUEST_PAPER = "REQUEST_PAPER"


class ControlCommandStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
