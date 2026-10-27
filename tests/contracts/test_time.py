from datetime import UTC, datetime, timedelta, timezone

import pytest

from sentinel_contracts.time import is_historical


def test_is_historical_uses_strict_24_hour_utc_boundary() -> None:
    occurred_at = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)

    assert not is_historical(occurred_at, occurred_at + timedelta(hours=24))
    assert is_historical(occurred_at, occurred_at + timedelta(hours=24, milliseconds=1))
    assert not is_historical(occurred_at, occurred_at + timedelta(hours=23))
    assert not is_historical(occurred_at + timedelta(seconds=1), occurred_at)


def test_is_historical_normalizes_aware_offsets_and_rejects_naive_values() -> None:
    occurred_at = datetime(2026, 10, 25, 12, tzinfo=timezone(timedelta(hours=3)))
    captured_at = datetime(2026, 10, 26, 9, 0, 0, 1, tzinfo=UTC)

    assert is_historical(occurred_at, captured_at)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_historical(datetime(2026, 10, 25, 9), captured_at)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_historical(occurred_at, datetime(2026, 10, 26, 9))
