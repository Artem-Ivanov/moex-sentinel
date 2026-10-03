"""Deterministic observation timing/order/scope boundaries without persistence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moex_sentinel.services.runtime_versions import WorkerVersionStore
from sentinel_contracts.runtime_versions import RuntimeVersion

NOW = datetime(2026, 10, 3, 12, 0, 0, 123000, tzinfo=UTC)


def metadata(version="1.2.3", **values):
    return RuntimeVersion(
        version=version,
        instance_id=uuid4(),
        environment=values.get("environment", "TEST"),
        access_mode=values.get("access_mode", "READ_ONLY"),
    )


def store():
    ticks = [0.0]
    return WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: NOW, monotonic_clock=lambda: ticks[0]), ticks


@pytest.mark.parametrize(
    ("elapsed", "observation", "version"),
    [(29.999, "OBSERVED", "1.2.3"), (30.0, "UNKNOWN", None), (30.001, "UNKNOWN", None)],
)
def test_ttl_before_exact_and_after_uses_monotonic_age(elapsed, observation, version):
    source, ticks = store()
    source.observe(metadata(), NOW)
    ticks[0] = elapsed
    result = source.snapshot()
    assert result.observation == observation
    assert result.version == version
    assert result.age_ms == int(elapsed * 1000)
    assert result.received_at == "2026-10-03T12:00:00.123Z"
    assert result.reason == ("OBSERVED" if version else "STALE")


@pytest.mark.parametrize("offset_ms", [-30_000, -30_001, 5001])
def test_stale_or_future_incoming_does_not_create_observation(offset_ms):
    source, _ = store()
    source.observe(metadata(), NOW + timedelta(milliseconds=offset_ms))
    assert source.snapshot().reason == "NOT_OBSERVED"


@pytest.mark.parametrize("offset_ms", [-29_999, 5000])
def test_incoming_at_accepted_boundaries(offset_ms):
    source, _ = store()
    source.observe(metadata(), NOW + timedelta(milliseconds=offset_ms))
    assert source.snapshot().reason == "OBSERVED"


@pytest.mark.parametrize("values", [{"environment": "PROD"}, {"access_mode": "TRADE"}])
def test_wrong_scope_never_creates_observation(values):
    source, _ = store()
    source.observe(metadata(**values), NOW)
    assert source.snapshot().reason == "NOT_OBSERVED"


@pytest.mark.parametrize("offset_ms", [0, -1])
def test_duplicate_or_older_even_new_instance_does_not_renew_ttl(offset_ms):
    source, ticks = store()
    source.observe(metadata(), NOW)
    ticks[0] = 29.0
    source.observe(metadata("9.9.9"), NOW + timedelta(milliseconds=offset_ms))
    assert source.snapshot().version == "1.2.3"
    ticks[0] = 30.0
    assert source.snapshot().reason == "STALE"


def test_new_instance_newer_timestamp_receives_new_server_timestamp_and_ttl():
    source, ticks = store()
    source.observe(metadata(), NOW - timedelta(seconds=1))
    ticks[0] = 29.0
    source.observe(metadata("9.9.9"), NOW)
    assert source.snapshot().version == "9.9.9"
    assert source.snapshot().age_ms == 0
    ticks[0] = 59.0
    assert source.snapshot().reason == "STALE"


def test_naive_timestamp_does_not_create_observation():
    source, _ = store()
    source.observe(metadata(), NOW.replace(tzinfo=None))
    assert source.snapshot().reason == "NOT_OBSERVED"
