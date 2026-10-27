from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_automaton.services import order_lifecycle

intent_deadline = order_lifecycle.intent_deadline

NOW = datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=UTC)


def intent(*, started_at=NOW, ttl=10):
    return SimpleNamespace(
        dispatch_started_at=started_at,
        strategy_snapshot={"order_ttl_seconds": ttl},
    )


def test_deadline_uses_durable_snapshot_with_utc_millisecond_precision():
    assert intent_deadline(intent(), now=NOW) == datetime(2026, 10, 18, 12, 0, 10, 123000, tzinfo=UTC)


def test_deadline_is_unknown_without_durable_ttl_or_for_future_timestamp():
    legacy = SimpleNamespace(dispatch_started_at=NOW, strategy_snapshot={})

    assert intent_deadline(legacy, now=NOW) is None
    assert intent_deadline(intent(started_at=NOW + timedelta(milliseconds=1)), now=NOW) is None


@pytest.mark.parametrize(
    ("started_at", "ttl"),
    [
        (None, 10),
        (NOW.replace(tzinfo=None), 10),
        (NOW, None),
        (NOW, 0),
        (NOW, -1),
        (NOW, "10"),
        (NOW, True),
    ],
)
def test_deadline_is_unknown_when_stored_proof_is_invalid(started_at, ttl):
    assert intent_deadline(intent(started_at=started_at, ttl=ttl), now=NOW) is None


def test_age_policy_expires_only_after_strictly_more_than_24_hours_from_created_at():
    record = SimpleNamespace(created_at=NOW - timedelta(hours=24, milliseconds=1))
    age_expired = getattr(order_lifecycle, "intent_age_expired", None)

    assert callable(age_expired), "order lifecycle must expose the 24-hour age policy"
    assert age_expired(record, now=NOW) is True
    assert age_expired(
        SimpleNamespace(created_at=NOW - timedelta(hours=24)), now=NOW
    ) is False


@pytest.mark.parametrize(
    "created_at",
    [None, NOW.replace(tzinfo=None), NOW + timedelta(milliseconds=1)],
)
def test_age_policy_does_not_expire_when_created_at_is_missing_invalid_or_future(created_at):
    age_expired = getattr(order_lifecycle, "intent_age_expired", None)
    assert callable(age_expired), "order lifecycle must expose the 24-hour age policy"
    assert age_expired(SimpleNamespace(created_at=created_at), now=NOW) is False


def test_age_policy_does_not_expire_without_utc_now():
    age_expired = getattr(order_lifecycle, "intent_age_expired", None)
    assert callable(age_expired), "order lifecycle must expose the 24-hour age policy"
    assert age_expired(
        SimpleNamespace(created_at=NOW - timedelta(days=1)),
        now=NOW.replace(tzinfo=None),
    ) is False
