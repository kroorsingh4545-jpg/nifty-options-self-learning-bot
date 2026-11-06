"""IST clock helpers. All persisted and logged timestamps are IST-aware."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Current time as a timezone-aware IST datetime."""
    return datetime.now(tz=IST)


def ensure_ist(value: datetime) -> datetime:
    """Convert an aware datetime to IST. Naive datetimes are rejected."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; attach a timezone")
    return value.astimezone(IST)


@dataclass(frozen=True)
class ClockOffset:
    """One comparison of this PC's clock with a reference clock.

    ``offset_seconds`` is local minus reference: positive means the PC clock
    is ahead. ``uncertainty_seconds`` bounds the measurement error (reference
    resolution plus half the round trip).
    """

    offset_seconds: float
    uncertainty_seconds: float
    source: str
    measured_at: datetime
