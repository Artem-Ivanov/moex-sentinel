"""Own broker iteration scheduling, serialization and Analytics client shutdown."""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from math import isfinite
from time import monotonic
from typing import Literal, Protocol

from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest
from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.trading_facts import AutomationCommand
from sentinel_contracts.worker_diagnostics import BrokerRuntimeDiagnostics
from trading_automaton.usecases.broker_iteration import RunBrokerIterationUsecase

LOGGER = logging.getLogger(__name__)
BROKER_RECOVERY_PROBE_SECONDS = 60.0


class AnalyticsPort(Protocol):
    async def snapshot(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot: ...
    async def close(self) -> None: ...


class AnalyticsBrokerRuntime:
    """Schedule a single iteration owner and close its client after in-flight work finishes."""

    def __init__(
        self,
        analytics: AnalyticsPort,
        iteration: RunBrokerIterationUsecase,
        *,
        tick_seconds: float = 1.0,
        retry_limit: int = 5,
        recovery_probe_seconds: float = BROKER_RECOVERY_PROBE_SECONDS,
        account_id: str = "",
        now=utc_now_ms,
    ) -> None:
        self._account_id = account_id
        self._now = now
        self._analytics = analytics
        self._iteration = iteration
        self._tick_seconds = tick_seconds
        if retry_limit < 0:
            raise ValueError("retry_limit must be nonnegative")
        if not isfinite(recovery_probe_seconds) or recovery_probe_seconds <= 0:
            raise ValueError("recovery_probe_seconds must be finite and positive")
        self._retry_limit = retry_limit
        self._recovery_probe_seconds = recovery_probe_seconds
        self._closed = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._broker_status: Literal["IDLE", "RUNNING", "BLOCKED", "UNKNOWN"] = "IDLE"
        self._decision_eligibility: Literal["READY", "NOT_READY", "UNKNOWN"] = "UNKNOWN"
        self._reason = "NO_ACTIVE_COMMAND"
        self._last_started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._last_success_at: datetime | None = None

    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None:
        """Publish command changes without blocking an iteration waiting on external I/O."""
        if self._account_id and any(command.account_id != self._account_id for command in commands):
            raise ValueError("Broker command account mismatch")
        await self._iteration.replace_commands(commands)

    async def run(self) -> None:
        """Retry transient outages slowly after the burst budget; keep permanent failures blocked until closed."""
        failures = 0
        cooling_down = False
        while not self._closed.is_set():
            started = monotonic()
            delay = self._tick_seconds
            try:
                await self.run_once()
            except BrokerOperationError as error:
                if not error.retryable:
                    LOGGER.error(  # noqa: TRY400 - keep broker exception text and metadata out of logs
                        "Broker preparation paused; runtime restart required",
                        extra={
                            "reason_code": "BROKER_PREPARATION_BLOCKED",
                            "data": {"retries": failures, "retryable": error.retryable},
                        },
                    )
                    await self._closed.wait()
                    return
                if failures >= self._retry_limit:
                    delay = self._recovery_probe_seconds
                    if not cooling_down:
                        LOGGER.warning(
                            "Broker preparation retry budget exhausted; recovery probes continue",
                            extra={
                                "reason_code": "BROKER_PREPARATION_COOLDOWN",
                                "data": {"retries": failures, "retry_delay_seconds": delay},
                            },
                        )
                        cooling_down = True
                else:
                    failures += 1
                    delay = 2 * failures - 1
                    LOGGER.warning(
                        "Broker preparation retry scheduled",
                        extra={
                            "reason_code": "BROKER_PREPARATION_RETRY",
                            "data": {"retry_attempt": failures, "retry_delay_seconds": delay},
                        },
                    )
            else:
                if cooling_down:
                    LOGGER.info(
                        "Broker preparation recovered",
                        extra={"reason_code": "BROKER_PREPARATION_RECOVERED"},
                    )
                    cooling_down = False
                failures = 0
                delay = max(0.0, self._tick_seconds - (monotonic() - started))
            with suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), timeout=delay)

    async def close(self) -> None:
        """Signal stop and wait for the running iteration before closing Analytics."""
        self._closed.set()
        async with self._run_lock:
            await self._analytics.close()

    async def run_once(self) -> None:
        """Serialize one finite application operation with shutdown."""
        async with self._run_lock:
            previous_timestamps = self._last_started_at, self._last_finished_at, self._last_success_at
            started = floor_utc_millisecond(self._now())
            self._last_started_at = started
            self._last_finished_at = None
            self._broker_status = "RUNNING"
            self._decision_eligibility = "UNKNOWN"
            self._reason = "ITERATION_RUNNING"
            try:
                outcome = await self._iteration.execute(is_closed=self._closed.is_set)
            except BrokerOperationError as error:
                self._last_finished_at = floor_utc_millisecond(self._now())
                self._decision_eligibility = "UNKNOWN"
                if error.retryable:
                    self._broker_status = "RUNNING"
                    self._reason = "BROKER_RETRYING"
                else:
                    self._broker_status = "BLOCKED"
                    self._reason = "PERMANENT_BROKER_ERROR"
                raise
            except Exception:
                self._last_finished_at = floor_utc_millisecond(self._now())
                self._broker_status = "UNKNOWN"
                self._decision_eligibility = "UNKNOWN"
                self._reason = "NOT_OBSERVED"
                raise
            self._last_finished_at = floor_utc_millisecond(self._now())
            if outcome == "NO_ACTIVE_COMMAND":
                self._broker_status = "IDLE"
                self._decision_eligibility = "UNKNOWN"
                self._reason = "NO_ACTIVE_COMMAND"
                self._last_started_at, self._last_finished_at, self._last_success_at = previous_timestamps
                return
            self._broker_status = "RUNNING"
            self._last_success_at = self._last_finished_at
            if outcome == "DECISION_FRAME_AVAILABLE":
                self._decision_eligibility = "READY"
                self._reason = "DECISION_FRAME_AVAILABLE"
            else:
                self._decision_eligibility = "NOT_READY"
                self._reason = "NO_DECISION_FRAME"

    def broker_runtime_diagnostics(self) -> BrokerRuntimeDiagnostics:
        return BrokerRuntimeDiagnostics(
            status=self._broker_status,
            decision_eligibility=self._decision_eligibility,
            reason=self._reason,
            last_started_at=self._last_started_at,
            last_finished_at=self._last_finished_at,
            last_success_at=self._last_success_at,
        )
