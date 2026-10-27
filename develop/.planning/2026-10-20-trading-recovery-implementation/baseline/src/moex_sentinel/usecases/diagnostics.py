"""Core diagnostics from existing readiness and captured runtime settings."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from moex_sentinel import __version__
from moex_sentinel.usecases.health import CheckReadinessUsecase
from sentinel_contracts.runtime_versions import ServiceVersions, VersionObservation
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment
from sentinel_contracts.worker_diagnostics import WorkerObservation


class CoreDiagnostics(BaseModel):
    """Safe readiness observations without transport or storage details."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    status: Literal["OK", "DOWN", "DEGRADED"]
    version: str
    database: Literal["ok", "error"]
    schema_status: Literal["compatible", "unknown", "not_ready"] = Field(alias="schema")
    reason: Literal["READY", "DATABASE_UNAVAILABLE", "SCHEMA_NOT_READY"]


class DiagnosticsRuntime(BaseModel):
    """Settings of this Core instance, not the observed strategy runtime."""

    model_config = ConfigDict(frozen=True)

    environment: BrokerEnvironment
    access_mode: BrokerAccessMode


class DiagnosticsStatus(BaseModel):
    """An incomplete global snapshot, with unobserved services explicit."""

    model_config = ConfigDict(frozen=True)

    service_versions: ServiceVersions = Field(default_factory=ServiceVersions)
    worker: WorkerObservation = Field(default_factory=lambda: WorkerObservation(reason="NOT_OBSERVED"))
    captured_at: str
    status: Literal["UNKNOWN", "DOWN", "DEGRADED"]
    core: CoreDiagnostics
    runtime: DiagnosticsRuntime
    awaiting_observations: tuple[str, ...] = (
        "worker",
        "broker",
        "analytics",
        "market",
        "outbox",
        "portfolio",
        "strategy",
    )


class GetDiagnosticsStatusUsecase:
    """Map readiness to operator diagnostics, then capture the completion time."""

    def __init__(
        self,
        readiness: CheckReadinessUsecase,
        environment: BrokerEnvironment,
        access_mode: BrokerAccessMode,
        *,
        clock: Callable[[], datetime] = utc_now_ms,
        worker_versions: Callable[[], VersionObservation] | None = None,
        worker_observations: Callable[[], tuple[VersionObservation, WorkerObservation]] | None = None,
    ) -> None:
        self._readiness = readiness
        self._runtime = DiagnosticsRuntime(environment=environment, access_mode=access_mode)
        self._clock = clock
        self._worker_versions = worker_versions
        self._worker_observations = worker_observations

    def execute(self, analytics: VersionObservation | None = None) -> DiagnosticsStatus:
        readiness = self._readiness.execute()
        if not readiness.database_ok:
            core = CoreDiagnostics(
                status="DOWN", version=__version__, database="error", schema="unknown", reason="DATABASE_UNAVAILABLE"
            )
        elif not readiness.schema_ok:
            core = CoreDiagnostics(
                status="DEGRADED", version=__version__, database="ok", schema="not_ready", reason="SCHEMA_NOT_READY"
            )
        else:
            core = CoreDiagnostics(status="OK", version=__version__, database="ok", schema="compatible", reason="READY")
        captured_at = floor_utc_millisecond(self._clock()).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        worker_version = self._worker_versions() if self._worker_versions else VersionObservation(reason="NOT_OBSERVED")
        worker = WorkerObservation(reason="NOT_OBSERVED")
        if self._worker_observations is not None:
            worker_version, worker = self._worker_observations()
        awaiting = ["worker", "broker", "analytics", "market", "outbox", "portfolio", "strategy"]
        if worker.observation == "OBSERVED":
            awaiting.remove("worker")
        if worker.outbox.observation == "OBSERVED":
            awaiting.remove("outbox")
        status = "UNKNOWN" if core.status == "OK" else core.status
        if status != "DOWN" and (worker.status == "DEGRADED" or worker.outbox.status == "DEGRADED"):
            status = "DEGRADED"
        return DiagnosticsStatus(
            service_versions=ServiceVersions(
                worker=worker_version,
                analytics=analytics or VersionObservation(reason="NOT_CONFIGURED"),
            ),
            captured_at=captured_at,
            worker=worker,
            awaiting_observations=tuple(awaiting),
            status=status,
            core=core,
            runtime=self._runtime,
        )
