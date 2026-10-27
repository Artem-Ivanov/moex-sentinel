"""Application readiness probes independent of HTTP and database implementation."""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict


class ReadinessResult(BaseModel):
    """Readiness flags with schema readiness contingent on database availability."""

    model_config = ConfigDict(frozen=True)

    database_ok: bool
    schema_ok: bool


class CheckReadinessUsecase:
    """Check database first and suppress technical probe errors in the public result."""

    def __init__(self, database_probe: Callable[[], bool], schema_probe: Callable[[], bool]) -> None:
        self._database_probe = database_probe
        self._schema_probe = schema_probe

    def execute(self) -> ReadinessResult:
        """Return readiness flags, skipping the schema probe when the database is unavailable."""
        try:
            database_ok = self._database_probe()
        except Exception:
            database_ok = False
        if not database_ok:
            return ReadinessResult(database_ok=False, schema_ok=False)
        try:
            schema_ok = self._schema_probe()
        except Exception:
            schema_ok = False
        return ReadinessResult(database_ok=True, schema_ok=schema_ok)
