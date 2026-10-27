"""UTC millisecond helpers shared by Core and the trading worker."""

from datetime import UTC, datetime, timedelta


def utc_now_ms() -> datetime:
    """Return current UTC time floored to database and timeline precision."""
    value = datetime.now(UTC)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def floor_utc_millisecond(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC and floor it to milliseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    normalized = value.astimezone(UTC)
    return normalized.replace(microsecond=(normalized.microsecond // 1000) * 1000)


def require_utc_millisecond(value: datetime) -> datetime:
    """Accept only explicit UTC timestamps whose precision is milliseconds."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use an explicit UTC offset.")
    if value.microsecond % 1000:
        raise ValueError("Timestamp precision must not exceed milliseconds.")
    return value.astimezone(UTC)


def is_historical(occurred_at: datetime, captured_at: datetime) -> bool:
    """Return whether a record is strictly more than 24 hours old at capture time."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (occurred_at, captured_at)):
        raise ValueError("Timestamps must be timezone-aware.")
    occurred_utc = occurred_at.astimezone(UTC)
    captured_utc = captured_at.astimezone(UTC)
    return captured_utc > occurred_utc + timedelta(hours=24)
