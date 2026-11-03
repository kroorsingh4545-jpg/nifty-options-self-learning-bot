"""IST clock helpers. All persisted and logged timestamps are IST-aware."""

from __future__ import annotations

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
