"""Deterministic observation timing/order/scope boundaries without persistence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moex_sentinel.services.runtime_versions import WorkerVersionStore
from sentinel_contracts.runtime_versions import RuntimeVersion
from sentinel_contracts.worker_diagnostics import OutboxDiagnostics, WorkerDiagnostics

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


def progress(count=1, completed=NOW, finished=NOW, result="COMPLETED", **queue):
    return WorkerDiagnostics(
        completed_iterations=count,
        last_completed_at=completed,
        last_finished_at=finished,
        last_result=result,
        error_code="ITERATION_FAILED" if result == "ERROR" else None,
        outbox=OutboxDiagnostics(observation="OBSERVED", reason="OBSERVED", pending_count=0, failed_count=0, **queue),
    )


def test_repeated_frozen_completion_does_not_refresh_completion_age():
    clock = [NOW]
    ticks = [0.0]
    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    identity = metadata()
    source.observe(identity, NOW, progress())
    clock[0] += timedelta(seconds=29)
    ticks[0] = 29.0
    source.observe(identity, clock[0], progress())
    ticks[0] = 30.0
    version, observed = source.diagnostics_snapshot()
    assert version.observation == "OBSERVED"
    assert observed.status == "DEGRADED"
    assert observed.reason == "CONTROL_STALLED"
    assert observed.completion_age_ms == 30000
    assert observed.age_ms == 1000


@pytest.mark.parametrize("invalid", ["rollback", "same_count_changed_time", "future_nested", "older_completion"])
def test_invalid_progress_does_not_refresh_any_observation(invalid):
    clock = [NOW]
    ticks = [0.0]
    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    identity = metadata()
    source.observe(identity, NOW, progress(count=2))
    clock[0] += timedelta(seconds=29)
    ticks[0] = 29.0
    values = {
        "rollback": progress(count=1),
        "same_count_changed_time": progress(count=2, completed=clock[0], finished=clock[0]),
        "future_nested": progress(
            count=3, completed=clock[0] + timedelta(seconds=1), finished=clock[0] + timedelta(seconds=1)
        ),
        "older_completion": progress(
            count=3, completed=NOW - timedelta(seconds=1), finished=NOW - timedelta(seconds=1)
        ),
    }
    source.observe(identity, clock[0], values[invalid])
    ticks[0] = 30.0
    version, observed = source.diagnostics_snapshot()
    assert version.reason == "STALE"
    assert observed.reason == "STALE"
    assert observed.completed_iterations is None
    assert observed.received_at == "2026-10-03T12:00:00.123Z"
    assert observed.outbox.pending_count is None


def test_new_instance_clears_old_progress_even_when_new_heartbeat_is_legacy():
    source, _ = store()
    source.observe(
        metadata(),
        NOW - timedelta(seconds=1),
        progress(completed=NOW - timedelta(seconds=1), finished=NOW - timedelta(seconds=1)),
    )
    assert source.diagnostics_snapshot()[1].completed_iterations == 1
    source.observe(metadata(), NOW)
    version, observed = source.diagnostics_snapshot()
    assert version.observation == "OBSERVED"
    assert observed.reason == "NOT_OBSERVED"
    assert observed.completed_iterations is None


@pytest.mark.parametrize(("result", "reason"), [("ERROR", "ITERATION_FAILED"), ("OUTBOX_BLOCKED", "OUTBOX_BLOCKED")])
def test_failed_finished_iteration_is_immediately_degraded(result, reason):
    source, _ = store()
    source.observe(metadata(), NOW, progress(count=0, completed=None, result=result))
    observed = source.diagnostics_snapshot()[1]
    assert observed.status == "DEGRADED"
    assert observed.reason == reason


@pytest.mark.parametrize(
    ("pending", "failed", "old_seconds", "reason", "status"),
    [
        (0, 0, None, "CLEAR", "OK"),
        (1, 0, 59.999, "PENDING", "OK"),
        (1, 0, 60, "OLD_PENDING", "DEGRADED"),
        (1, 1, 1, "FAILED", "DEGRADED"),
    ],
)
def test_outbox_public_thresholds(pending, failed, old_seconds, reason, status):
    source, _ = store()
    payload = progress().model_copy(
        update={
            "outbox": OutboxDiagnostics(
                observation="OBSERVED",
                reason="OBSERVED",
                pending_count=pending,
                failed_count=failed,
                oldest_pending_at=NOW - timedelta(seconds=old_seconds) if pending else None,
                max_retry_count=2 if pending else None,
            )
        }
    )
    source.observe(metadata(), NOW, payload)
    queue = source.diagnostics_snapshot()[1].outbox
    assert (queue.reason, queue.status) == (reason, status)


def test_repeated_completion_age_ignores_server_wall_clock_rollback():
    ticks, clock = [0.0], [NOW]
    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    identity = metadata()
    completed = NOW - timedelta(seconds=10)
    source.observe(identity, NOW - timedelta(seconds=2), progress(completed=completed, finished=completed))
    ticks[0] = 5
    clock[0] = NOW - timedelta(seconds=4)
    source.observe(identity, NOW - timedelta(seconds=1), progress(completed=completed, finished=completed))
    result = source.diagnostics_snapshot()[1]
    assert result.completion_age_ms == 15000
    assert result.age_ms == 0


def test_legacy_same_instance_heartbeat_does_not_renew_control_observation():
    clock, ticks = [NOW], [0.0]
    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    identity = metadata()
    source.observe(identity, NOW, progress())
    clock[0] += timedelta(seconds=29)
    ticks[0] = 29
    source.observe(identity, clock[0])
    ticks[0] = 30
    version, worker = source.diagnostics_snapshot()
    assert version.observation == "OBSERVED"
    assert worker.reason == "STALE"
    assert worker.outbox.reason == "STALE"


def test_new_instance_starts_with_its_own_counter_and_completion_age():
    clock, ticks = [NOW], [0.0]
    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    source.observe(metadata(), NOW, progress(count=50))
    clock[0] += timedelta(seconds=29)
    ticks[0] = 29
    identity = metadata()
    source.observe(identity, clock[0], progress(completed=clock[0], finished=clock[0]))
    worker = source.diagnostics_snapshot()[1]
    assert worker.instance_id == identity.instance_id
    assert worker.completed_iterations == 1
    assert worker.completion_age_ms == 0
