"""Readiness mapping and snapshot clock boundaries."""

from datetime import datetime, timedelta, timezone

import pytest

from moex_sentinel.usecases.diagnostics import GetDiagnosticsStatusUsecase
from moex_sentinel.usecases.health import CheckReadinessUsecase
from sentinel_contracts.runtime_versions import VersionObservation
from sentinel_contracts.worker_diagnostics import OutboxObservation, WorkerObservation


@pytest.mark.parametrize(
    ("database", "schema", "expected"),
    [
        (True, True, ("UNKNOWN", "OK", "ok", "compatible", "READY")),
        (False, True, ("DOWN", "DOWN", "error", "unknown", "DATABASE_UNAVAILABLE")),
        (True, False, ("DEGRADED", "DEGRADED", "ok", "not_ready", "SCHEMA_NOT_READY")),
    ],
)
def test_snapshot_maps_readiness_and_captures_clock_after_probes(database, schema, expected) -> None:
    events = []

    def database_probe() -> bool:
        events.append("database")
        return database

    def schema_probe() -> bool:
        events.append("schema")
        return schema

    def clock() -> datetime:
        events.append("clock")
        return datetime(2026, 10, 3, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))

    usecase = GetDiagnosticsStatusUsecase(
        CheckReadinessUsecase(database_probe, schema_probe), "TEST", "READ_ONLY", clock=clock
    )
    result = usecase.execute().model_dump(by_alias=True)
    assert result["captured_at"] == "2026-10-03T09:00:00.123Z"
    assert events == (["database", "schema", "clock"] if database else ["database", "clock"])
    assert (
        result["status"],
        result["core"]["status"],
        result["core"]["database"],
        result["core"]["schema"],
        result["core"]["reason"],
    ) == expected
    assert result["runtime"] == {"environment": "TEST", "access_mode": "READ_ONLY"}


@pytest.mark.parametrize("failed_probe", ["database", "schema"])
def test_probe_exceptions_cannot_escape_the_snapshot(failed_probe: str) -> None:
    def failing() -> bool:
        raise RuntimeError("secret connection details")

    readiness = CheckReadinessUsecase(
        failing if failed_probe == "database" else lambda: True,
        failing if failed_probe == "schema" else lambda: True,
    )
    result = GetDiagnosticsStatusUsecase(readiness, "TEST", "READ_ONLY").execute()
    assert result.status == ("DOWN" if failed_probe == "database" else "DEGRADED")
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize(("database", "expected"), [(True, "DEGRADED"), (False, "DOWN")])
def test_worker_degradation_is_visible_but_database_down_has_priority(database, expected):
    worker = WorkerObservation(
        observation="OBSERVED",
        status="DEGRADED",
        reason="ITERATION_FAILED",
        outbox=OutboxObservation(reason="READ_FAILED"),
    )
    usecase = GetDiagnosticsStatusUsecase(
        CheckReadinessUsecase(lambda: database, lambda: True),
        "TEST",
        "READ_ONLY",
        worker_observations=lambda: (VersionObservation(reason="OBSERVED"), worker),
    )
    result = usecase.execute()
    assert result.status == expected
    assert "worker" not in result.awaiting_observations
    assert "outbox" in result.awaiting_observations


def test_failed_queue_degrades_global_snapshot_even_with_completed_control():
    worker = WorkerObservation(
        observation="OBSERVED",
        status="OK",
        reason="CONTROL_PROGRESS",
        outbox=OutboxObservation(observation="OBSERVED", status="DEGRADED", reason="FAILED", failed_count=1),
    )
    result = GetDiagnosticsStatusUsecase(
        CheckReadinessUsecase(lambda: True, lambda: True),
        "TEST",
        "READ_ONLY",
        worker_observations=lambda: (VersionObservation(reason="OBSERVED"), worker),
    ).execute()
    assert result.status == "DEGRADED"
    assert "worker" not in result.awaiting_observations
    assert "outbox" not in result.awaiting_observations
