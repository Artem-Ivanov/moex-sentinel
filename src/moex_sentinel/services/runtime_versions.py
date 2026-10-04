"""One bounded, thread-safe Worker observation belonging to this Core runtime."""

from collections.abc import Callable
from datetime import datetime
from threading import Lock
from time import monotonic

from sentinel_contracts.runtime_versions import RuntimeVersion, VersionObservation
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment
from sentinel_contracts.worker_diagnostics import OutboxObservation, WorkerDiagnostics, WorkerObservation


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WorkerVersionStore:
    def __init__(
        self,
        environment: BrokerEnvironment,
        access_mode: BrokerAccessMode,
        *,
        clock: Callable[[], datetime] = utc_now_ms,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._environment = environment
        self._access_mode = access_mode
        self._clock = clock
        self._monotonic = monotonic_clock
        self._lock = Lock()
        self._version: RuntimeVersion | None = None
        self._occurred_at: datetime | None = None
        self._received_at: str | None = None
        self._received_monotonic = 0.0
        self._diagnostics: WorkerDiagnostics | None = None
        self._diagnostics_received_at: str | None = None
        self._diagnostics_monotonic = 0.0
        self._completion_age_ms: int | None = None
        self._completion_monotonic = 0.0
        self._oldest_pending_age_ms: int | None = None
        self._oldest_pending_monotonic = 0.0

    def observe(
        self, metadata: RuntimeVersion, occurred_at: datetime, diagnostics: WorkerDiagnostics | None = None
    ) -> None:
        if metadata.environment != self._environment or metadata.access_mode != self._access_mode:
            return
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            return
        timestamp = floor_utc_millisecond(occurred_at)
        with self._lock:
            now = floor_utc_millisecond(self._clock())
            age_ms = (now - timestamp).total_seconds() * 1000
            if age_ms >= 30_000 or age_ms < -5000:
                return
            if self._occurred_at is not None and timestamp <= self._occurred_at:
                return
            same_instance = self._version is not None and self._version.instance_id == metadata.instance_id
            previous = self._diagnostics if same_instance else None
            if diagnostics is not None and not self._valid_progress(diagnostics, previous, timestamp):
                return
            tick = self._monotonic()
            if not same_instance:
                self._diagnostics = None
                self._diagnostics_received_at = None
                self._completion_age_ms = None
                self._oldest_pending_age_ms = None
            if diagnostics is not None:
                if previous is None or diagnostics.last_completed_at != previous.last_completed_at:
                    self._completion_age_ms = self._source_age(now, diagnostics.last_completed_at)
                    self._completion_monotonic = tick
                if previous is None or diagnostics.outbox.oldest_pending_at != previous.outbox.oldest_pending_at:
                    self._oldest_pending_age_ms = self._source_age(now, diagnostics.outbox.oldest_pending_at)
                    self._oldest_pending_monotonic = tick
                self._diagnostics = diagnostics
                self._diagnostics_received_at = _timestamp(now)
                self._diagnostics_monotonic = tick
            self._version = metadata
            self._occurred_at = timestamp
            self._received_at = _timestamp(now)
            self._received_monotonic = tick

    @staticmethod
    def _source_age(now: datetime, value: datetime | None) -> int | None:
        return None if value is None else max(0, int((now - value).total_seconds() * 1000))

    @staticmethod
    def _valid_progress(value: WorkerDiagnostics, previous: WorkerDiagnostics | None, heartbeat: datetime) -> bool:
        if value.last_finished_at > heartbeat:
            return False
        if value.outbox.oldest_pending_at is not None and value.outbox.oldest_pending_at > heartbeat:
            return False
        if previous is None:
            return True
        if (
            value.completed_iterations < previous.completed_iterations
            or value.last_finished_at < previous.last_finished_at
        ):
            return False
        if value.completed_iterations == previous.completed_iterations:
            return value.last_completed_at == previous.last_completed_at
        return previous.last_completed_at is None or (
            value.last_completed_at is not None and value.last_completed_at >= previous.last_completed_at
        )

    def snapshot(self) -> VersionObservation:
        with self._lock:
            return self._version_snapshot(self._monotonic())

    def diagnostics_snapshot(self) -> tuple[VersionObservation, WorkerObservation]:
        """Capture version and control/outbox at one lock and monotonic instant."""
        with self._lock:
            tick = self._monotonic()
            return self._version_snapshot(tick), self._worker_snapshot(tick)

    def _version_snapshot(self, tick: float) -> VersionObservation:
        if self._version is None:
            return VersionObservation(reason="NOT_OBSERVED")
        age_ms = max(0, int((tick - self._received_monotonic) * 1000))
        return VersionObservation(
            observation="UNKNOWN" if age_ms >= 30_000 else "OBSERVED",
            version=None if age_ms >= 30_000 else self._version.version,
            received_at=self._received_at,
            age_ms=age_ms,
            reason="STALE" if age_ms >= 30_000 else "OBSERVED",
        )

    def _worker_snapshot(self, tick: float) -> WorkerObservation:
        value = self._diagnostics
        if value is None:
            return WorkerObservation(reason="NOT_OBSERVED")
        age_ms = max(0, int((tick - self._diagnostics_monotonic) * 1000))
        if age_ms >= 30_000:
            return WorkerObservation(
                reason="STALE",
                received_at=self._diagnostics_received_at,
                age_ms=age_ms,
                outbox=OutboxObservation(reason="STALE"),
            )
        completion_age = self._anchored_age(self._completion_age_ms, self._completion_monotonic, tick)
        if value.last_result == "ERROR":
            reason = "ITERATION_FAILED"
        elif value.last_result == "OUTBOX_BLOCKED":
            reason = "OUTBOX_BLOCKED"
        elif completion_age is None or completion_age >= 30_000:
            reason = "CONTROL_STALLED"
        else:
            reason = "CONTROL_PROGRESS"
        return WorkerObservation(
            observation="OBSERVED",
            status="OK" if reason == "CONTROL_PROGRESS" else "DEGRADED",
            reason=reason,
            instance_id=self._version.instance_id if self._version else None,
            received_at=self._diagnostics_received_at,
            age_ms=age_ms,
            completed_iterations=value.completed_iterations,
            last_completed_at=_timestamp(value.last_completed_at),
            completion_age_ms=completion_age,
            last_finished_at=_timestamp(value.last_finished_at),
            last_result=value.last_result,
            error_code=value.error_code,
            outbox=self._outbox_snapshot(value, tick),
        )

    @staticmethod
    def _anchored_age(initial: int | None, started: float, tick: float) -> int | None:
        return None if initial is None else initial + max(0, int((tick - started) * 1000))

    def _outbox_snapshot(self, value: WorkerDiagnostics, tick: float) -> OutboxObservation:
        queue = value.outbox
        if queue.observation == "UNKNOWN":
            return OutboxObservation(reason="READ_FAILED")
        oldest_age = self._anchored_age(self._oldest_pending_age_ms, self._oldest_pending_monotonic, tick)
        if queue.failed_count:
            reason = "FAILED"
        elif oldest_age is not None and oldest_age >= 60_000:
            reason = "OLD_PENDING"
        elif queue.pending_count:
            reason = "PENDING"
        else:
            reason = "CLEAR"
        return OutboxObservation(
            observation="OBSERVED",
            status="DEGRADED" if reason in {"FAILED", "OLD_PENDING"} else "OK",
            reason=reason,
            pending_count=queue.pending_count,
            failed_count=queue.failed_count,
            oldest_pending_at=_timestamp(queue.oldest_pending_at),
            oldest_pending_age_ms=oldest_age,
            max_retry_count=queue.max_retry_count,
        )
