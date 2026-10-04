"""Bounded safe diagnostics reject inconsistent progress and unknown queue values."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel_contracts.worker_diagnostics import OutboxDiagnostics, WorkerDiagnostics

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


def test_unknown_outbox_cannot_claim_zero_counts():
    with pytest.raises(ValidationError):
        OutboxDiagnostics(reason="READ_FAILED", pending_count=0, failed_count=0)


@pytest.mark.parametrize("field", ["completed_iterations", "pending_count", "failed_count", "max_retry_count"])
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
