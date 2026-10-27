"""Bounded safe diagnostics reject inconsistent progress and unknown queue values."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sentinel_contracts.worker_diagnostics import (
    BrokerRuntimeDiagnostics,
    CommandSyncDiagnostics,
    CommandSyncRow,
    OutboxDiagnostics,
    WorkerDiagnostics,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def test_completed_progress_requires_a_completion_timestamp():
    with pytest.raises(ValidationError):
        WorkerDiagnostics(
            completed_iterations=1,
            last_completed_at=None,
            last_finished_at=NOW,
            last_result="COMPLETED",
            error_code=None,
            outbox=OutboxDiagnostics(reason="READ_FAILED"),
        )


def test_legacy_worker_payload_defaults_broker_runtime_to_unknown():
    worker = WorkerDiagnostics.model_validate(
        {
            "completed_iterations": 0,
            "last_completed_at": None,
            "last_finished_at": NOW,
            "last_result": "ERROR",
            "error_code": "ITERATION_FAILED",
            "outbox": {"reason": "READ_FAILED"},
        }
    )

    assert worker.broker_runtime.status == "UNKNOWN"
    assert worker.broker_runtime.decision_eligibility == "UNKNOWN"


def test_broker_runtime_is_idle_without_a_command_and_does_not_claim_eligibility():
    value = BrokerRuntimeDiagnostics(status="IDLE", reason="NO_ACTIVE_COMMAND")

    assert value.decision_eligibility == "UNKNOWN"
    assert value.last_success_at is None


def test_broker_runtime_rejects_unsafe_reason_and_inconsistent_timestamps():
    with pytest.raises(ValidationError):
        BrokerRuntimeDiagnostics(status="BLOCKED", reason="SDK: token=secret")
    with pytest.raises(ValidationError):
        BrokerRuntimeDiagnostics(
            status="RUNNING",
            reason="ITERATION_RUNNING",
            last_finished_at=NOW,
            last_success_at=NOW + timedelta(milliseconds=1),
        )


def test_unknown_outbox_cannot_claim_zero_counts():
    with pytest.raises(ValidationError):
        OutboxDiagnostics(reason="READ_FAILED", pending_count=0, failed_count=0)


@pytest.mark.parametrize(
    "field",
    ["completed_iterations", "pending_count", "failed_count", "max_retry_count"],
)
def test_negative_or_coerced_counts_are_rejected(field):
    for invalid in (-1, True, "1"):
        if field == "completed_iterations":
            with pytest.raises(ValidationError):
                WorkerDiagnostics(
                    completed_iterations=invalid,
                    last_completed_at=NOW,
                    last_finished_at=NOW,
                    last_result="COMPLETED",
                    outbox=OutboxDiagnostics(reason="READ_FAILED"),
                )
        else:
            with pytest.raises(ValidationError):
                OutboxDiagnostics(reason="READ_FAILED", **{field: invalid})


@pytest.mark.parametrize("field", ["last_completed_at", "last_finished_at"])
def test_naive_progress_time_is_rejected(field):
    values = {
        "completed_iterations": 1,
        "last_completed_at": NOW,
        "last_finished_at": NOW,
        "last_result": "COMPLETED",
        "outbox": OutboxDiagnostics(reason="READ_FAILED"),
    }
    values[field] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        WorkerDiagnostics(**values)


def test_command_sync_projection_is_bounded_and_legacy_payload_remains_optional():
    row = CommandSyncRow(
        automation_id="00000000-0000-4000-8000-000000000501",
        state="IN_WORK",
        revision=3,
        last_sequence_number=12,
        binding_complete=True,
        bootstrap=False,
        active_intent_state=None,
        active_intent_terminal_present=None,
        pending_fact_count=0,
        failed_fact_count=0,
        monitored=True,
        grouped=True,
        exclusion_reason=None,
    )
    projection = CommandSyncDiagnostics(
        observation="OBSERVED",
        reason="OBSERVED",
        broker_id="00000000-0000-4000-8000-000000000001",
        captured_at=NOW,
        complete=True,
        truncated=False,
        cached_count=1,
        claimed_count=0,
        monitored_count=1,
        grouped_count=1,
        publication_observation="OBSERVED",
        rows=(row,),
    )
    worker = WorkerDiagnostics.model_validate(
        {
            "completed_iterations": 0,
            "last_completed_at": None,
            "last_finished_at": NOW,
            "last_result": "ERROR",
            "error_code": "ITERATION_FAILED",
            "outbox": {"reason": "READ_FAILED"},
            "command_sync": projection.model_dump(mode="json"),
        }
    )
    legacy = WorkerDiagnostics.model_validate(
        {
            "completed_iterations": 0,
            "last_completed_at": None,
            "last_finished_at": NOW,
            "last_result": "ERROR",
            "error_code": "ITERATION_FAILED",
            "outbox": {"reason": "READ_FAILED"},
        }
    )

    assert worker.command_sync == projection
    assert legacy.command_sync is None
    assert "token" not in str(projection.model_dump(mode="json"))
    assert "amount" not in projection.model_dump(mode="json")


def test_command_sync_projection_rejects_unbounded_rows_and_unscoped_claims():
    with pytest.raises(ValidationError):
        CommandSyncDiagnostics(
            observation="OBSERVED",
            reason="OBSERVED",
            broker_id="00000000-0000-4000-8000-000000000001",
            captured_at=NOW,
            complete=True,
            truncated=False,
            cached_count=101,
            claimed_count=0,
            monitored_count=0,
            grouped_count=0,
            rows=tuple(
                CommandSyncRow(
                    automation_id=f"00000000-0000-4000-8000-{index:012d}",
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    binding_complete=True,
                    bootstrap=False,
                    active_intent_state=None,
                    active_intent_terminal_present=None,
                    pending_fact_count=0,
                    failed_fact_count=0,
                    monitored=False,
                    grouped=False,
                    exclusion_reason="NOT_MONITORED",
                )
                for index in range(101)
            ),
        )
    with pytest.raises(ValidationError):
        CommandSyncDiagnostics(
            observation="UNKNOWN",
            reason="UNCONFIGURED_BROKER",
            broker_id=None,
            captured_at=None,
            complete=False,
            truncated=False,
            cached_count=0,
            claimed_count=0,
            monitored_count=0,
            grouped_count=0,
            rows=(),
        )
