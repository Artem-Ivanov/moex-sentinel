"""Readiness mapping and snapshot clock boundaries."""

from datetime import datetime, timedelta, timezone

import pytest

from moex_sentinel.usecases.diagnostics import GetDiagnosticsStatusUsecase
from moex_sentinel.usecases.health import CheckReadinessUsecase


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
