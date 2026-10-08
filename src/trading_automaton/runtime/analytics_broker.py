"""Own broker iteration scheduling, serialization and Analytics client shutdown."""

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from typing import Protocol

from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest
from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.trading_facts import AutomationCommand
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
        account_id: str = "",
    ) -> None:
        self._account_id = account_id
        self._analytics = analytics
        self._iteration = iteration
        self._tick_seconds = tick_seconds
        if retry_limit < 0:
            raise ValueError("retry_limit must be nonnegative")
        self._retry_limit = retry_limit
        self._closed = asyncio.Event()
        self._run_lock = asyncio.Lock()

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
                    delay = BROKER_RECOVERY_PROBE_SECONDS
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
            await self._iteration.execute(is_closed=self._closed.is_set)
