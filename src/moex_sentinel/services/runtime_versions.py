"""One bounded, thread-safe version observation belonging to this Core runtime."""

from collections.abc import Callable
from datetime import datetime
from threading import Lock
from time import monotonic

from sentinel_contracts.runtime_versions import RuntimeVersion, VersionObservation
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment


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

    def observe(self, metadata: RuntimeVersion, occurred_at: datetime) -> None:
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
            self._version = metadata
            self._occurred_at = timestamp
            self._received_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            self._received_monotonic = self._monotonic()

    def snapshot(self) -> VersionObservation:
        with self._lock:
            if self._version is None:
                return VersionObservation(reason="NOT_OBSERVED")
            age_ms = max(0, int((self._monotonic() - self._received_monotonic) * 1000))
            return VersionObservation(
                observation="UNKNOWN" if age_ms >= 30_000 else "OBSERVED",
                version=None if age_ms >= 30_000 else self._version.version,
                received_at=self._received_at,
                age_ms=age_ms,
                reason="STALE" if age_ms >= 30_000 else "OBSERVED",
            )
