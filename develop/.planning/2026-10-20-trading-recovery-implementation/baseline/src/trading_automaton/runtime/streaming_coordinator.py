"""Coordinate one persistent streaming runtime per active broker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic as system_monotonic
from typing import Literal, Protocol

from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading_facts import AutomationCommand
from sentinel_contracts.worker_diagnostics import (
    BrokerRuntimeDiagnostics,
    CommandSyncDiagnostics,
    IterationResult,
    OutboxDiagnostics,
    WorkerDiagnostics,
)
from trading_automaton.usecases.synchronize_runtime import (
    SynchronizeTradingRuntimeUsecase,
)

LOGGER = logging.getLogger(__name__)

_SQLITE_CONSTRAINT_MARKERS = {
    "fact_outbox.automation_id, fact_outbox.sequence_number": "uq_fact_outbox_sequence",
    "account_commission_profiles.broker_id, account_commission_profiles.account_id, "
    "account_commission_profiles.instrument_type, account_commission_profiles.currency": (
        "pk_account_commission_profiles"
    ),
    "business_audit_events.event_id": "pk_business_audit_events",
    "broker_intents.idempotency_key": "pk_broker_intents",
    "trade_decisions.id": "pk_trade_decisions",
    "trade_lots.source_intent_id": "uq_trade_lot_source_intent",
    "lot_allocations.sell_intent_id, lot_allocations.lot_id": "uq_sell_lot_allocation",
}


def _exception_type_names(error: BaseException) -> tuple[str, ...]:
    names: set[str] = set()
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        else:
            names.add(type(current).__name__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return tuple(sorted(names))


def _sqlite_error_names(error: BaseException) -> tuple[str, ...]:
    names: set[str] = set()
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        name = getattr(current, "sqlite_errorname", None)
        if (
            isinstance(name, str)
            and name.startswith("SQLITE_")
            and all(character.isalnum() or character == "_" for character in name)
        ):
            names.add(name)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        origin = getattr(current, "orig", None)
        if isinstance(origin, BaseException):
            pending.append(origin)
    return tuple(sorted(names))


def _sqlite_constraint_names(error: BaseException) -> tuple[str, ...]:
    names: set[str] = set()
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current)
        names.update(name for marker, name in _SQLITE_CONSTRAINT_MARKERS.items() if marker in message)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        origin = getattr(current, "orig", None)
        if isinstance(origin, BaseException):
            pending.append(origin)
    return tuple(sorted(names))


class CoordinatorCorePort(Protocol):
    def broker_connection(self, broker_id: str) -> BrokerConnection: ...
    def heartbeat(
        self,
        worker_id: str,
        occurred_at: datetime,
        diagnostics: WorkerDiagnostics | None = None,
    ) -> None: ...


class RuntimePort(Protocol):
    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    def broker_runtime_diagnostics(self) -> BrokerRuntimeDiagnostics: ...


class RuntimeBundlePort(Protocol):
    broker_id: str
    runtime: RuntimePort

    async def close(self) -> None: ...


class StreamingRuntimeCoordinator:
    """Own broker bundles, tasks and heartbeat scheduling around a finite control operation."""

    def __init__(
        self,
        iteration: SynchronizeTradingRuntimeUsecase,
        core: CoordinatorCorePort,
        builder: Callable[[BrokerConnection], Awaitable[RuntimeBundlePort]],
        *,
        worker_id: str,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = system_monotonic,
        heartbeat_interval_seconds: float = 3.0,
        outbox_diagnostics: Callable[[], OutboxDiagnostics] | None = None,
        command_sync_diagnostics: (
            Callable[[set[str] | None, int, Literal["REPLACE_FAILED", "SYNC_FAILED"] | None], CommandSyncDiagnostics]
            | None
        ) = None,
    ) -> None:
        self._outbox_diagnostics = outbox_diagnostics
        self._command_sync_diagnostics = command_sync_diagnostics
        self._command_sync_snapshot: CommandSyncDiagnostics | None = None
        self._completed_iterations = 0
        self._last_completed_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._last_result: IterationResult | None = None
        self._iteration = iteration
        self._core = core
        self._builder = builder
        self._worker_id = worker_id
        self._now = now
        self._monotonic = monotonic
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._next_heartbeat_at = 0.0
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._bundles: dict[str, RuntimeBundlePort] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run_iteration(self) -> None:
        """Run command synchronization with this runtime's publication and heartbeat capabilities."""
        cancelled = False
        self._command_sync_snapshot = None
        try:
            outcome = await self._iteration.execute(replace_broker_commands=self._replace_broker_commands)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            self._last_result = "ERROR"
            self._last_finished_at = floor_utc_millisecond(self._now())
            if self._command_sync_snapshot is None:
                await self._capture_command_sync(None, "SYNC_FAILED")
            raise
        else:
            self._last_result = outcome
            self._last_finished_at = floor_utc_millisecond(self._now())
            if outcome == "COMPLETED":
                self._completed_iterations += 1
                self._last_completed_at = self._last_finished_at
        finally:
            if not cancelled:
                if self._command_sync_snapshot is None:
                    await self._capture_command_sync(None, "SYNC_FAILED")
                self._schedule_heartbeat()

    async def _capture_command_sync(
        self,
        grouped_automation_ids: set[str] | None,
        publication_reason: Literal["REPLACE_FAILED", "SYNC_FAILED"] | None,
    ) -> None:
        if self._command_sync_diagnostics is None:
            return
        try:
            self._command_sync_snapshot = await asyncio.to_thread(
                self._command_sync_diagnostics,
                grouped_automation_ids,
                self._iteration.last_claimed_count,
                publication_reason,
            )
        except Exception as error:
            LOGGER.warning("Command sync observation failed: %s", type(error).__name__)
            self._command_sync_snapshot = CommandSyncDiagnostics(reason="READ_FAILED")

    async def close(self) -> None:
        """Close broker resources, then await broker and heartbeat tasks before clearing ownership."""
        await asyncio.gather(*(bundle.close() for bundle in self._bundles.values()))
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._heartbeat_task is not None:
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        self._bundles.clear()
        self._tasks.clear()

    def _schedule_heartbeat(self) -> None:
        """Schedule at most one heartbeat task when the monotonic interval has elapsed."""
        current = self._monotonic()
        if current < self._next_heartbeat_at:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._next_heartbeat_at = current + self._heartbeat_interval_seconds
        if self._last_finished_at is None or self._last_result is None:
            return
        self._heartbeat_task = asyncio.create_task(
            asyncio.to_thread(
                self._send_heartbeat,
                self._completed_iterations,
                self._last_completed_at,
                self._last_finished_at,
                self._last_result,
                self._broker_runtime_diagnostics(),
                self._command_sync_snapshot,
            )
        )

    def _broker_runtime_diagnostics(self) -> BrokerRuntimeDiagnostics:
        values = []
        for bundle in self._bundles.values():
            getter = getattr(bundle.runtime, "broker_runtime_diagnostics", None)
            if getter is None:
                values.append(BrokerRuntimeDiagnostics(reason="NOT_OBSERVED"))
                continue
            try:
                value = getter()
                values.append(value if isinstance(value, BrokerRuntimeDiagnostics) else BrokerRuntimeDiagnostics())
            except Exception:
                values.append(BrokerRuntimeDiagnostics())
        if not values:
            return BrokerRuntimeDiagnostics(status="IDLE", reason="NO_ACTIVE_COMMAND")

        blocked = next((item for item in values if item.status == "BLOCKED"), None)
        if blocked is not None:
            status = "BLOCKED"
            eligibility = "UNKNOWN"
            reason = blocked.reason
        elif any(item.status == "UNKNOWN" for item in values):
            status = "UNKNOWN"
            eligibility = "UNKNOWN"
            reason = "NOT_OBSERVED"
        elif any(item.status == "RUNNING" for item in values):
            status = "RUNNING"
            eligibility = "NOT_READY" if any(item.decision_eligibility == "NOT_READY" for item in values) else "UNKNOWN"
            reason = (
                "NO_DECISION_FRAME"
                if eligibility == "NOT_READY"
                else next(
                    (item.reason for item in values if item.reason == "BROKER_RETRYING"),
                    "ITERATION_RUNNING",
                )
            )
        else:
            status = "IDLE"
            eligibility = "UNKNOWN"
            reason = "NO_ACTIVE_COMMAND"

        if status != "BLOCKED" and all(item.decision_eligibility == "READY" for item in values):
            eligibility = "READY"
            reason = "DECISION_FRAME_AVAILABLE"
        elif status == "RUNNING" and any(item.decision_eligibility == "NOT_READY" for item in values):
            eligibility = "NOT_READY"
            reason = "NO_DECISION_FRAME"
        candidates = [item for item in values if item.status == "BLOCKED"] if status == "BLOCKED" else values
        representative = max(
            candidates,
            key=lambda item: (
                item.last_started_at or datetime.min.replace(tzinfo=UTC),
                item.last_finished_at or datetime.min.replace(tzinfo=UTC),
            ),
        )
        return BrokerRuntimeDiagnostics(
            status=status,
            decision_eligibility=eligibility,
            reason=reason,
            last_started_at=representative.last_started_at,
            last_finished_at=representative.last_finished_at,
            last_success_at=representative.last_success_at,
        )

    def _send_heartbeat(
        self,
        completed_iterations: int,
        last_completed_at: datetime | None,
        last_finished_at: datetime,
        last_result: IterationResult,
        broker_runtime: BrokerRuntimeDiagnostics,
        command_sync: CommandSyncDiagnostics | None,
    ) -> None:
        """Sample the local queue only at heartbeat cadence, isolating diagnostics failures."""
        outbox = OutboxDiagnostics(reason="READ_FAILED")
        if self._outbox_diagnostics is not None:
            try:
                outbox = self._outbox_diagnostics()
            except Exception as error:
                LOGGER.warning("Outbox observation failed: %s", type(error).__name__)
        try:
            self._core.heartbeat(
                self._worker_id,
                self._now(),
                WorkerDiagnostics(
                    completed_iterations=completed_iterations,
                    last_completed_at=last_completed_at,
                    last_finished_at=last_finished_at,
                    last_result=last_result,
                    error_code="ITERATION_FAILED" if last_result == "ERROR" else None,
                    outbox=outbox,
                    broker_runtime=broker_runtime,
                    command_sync=command_sync,
                ),
            )
        except Exception as error:
            LOGGER.warning("Heartbeat delivery failed: %s", type(error).__name__)

    async def _replace_broker_commands(self, grouped: dict[str, list[AutomationCommand]]) -> None:
        """Retire absent or failed bundles and publish commands to each remaining broker runtime."""
        published: set[str] = set()
        replacement_complete = False
        try:
            for broker_id in tuple(self._bundles):
                if broker_id not in grouped:
                    await self._bundles.pop(broker_id).close()
                    removed_task = self._tasks.pop(broker_id)
                    await asyncio.gather(removed_task, return_exceptions=True)
            for broker_id, commands in grouped.items():
                bundle = self._bundles.get(broker_id)
                task = self._tasks.get(broker_id)
                if bundle is not None and task is not None and task.done():
                    failure = task.exception()
                    if failure is not None:
                        failure_types = ",".join(_exception_type_names(failure))
                        sqlite_errors = ",".join(_sqlite_error_names(failure))
                        sqlite_detail = f" sqlite_error={sqlite_errors}" if sqlite_errors else ""
                        constraint_names = ",".join(_sqlite_constraint_names(failure))
                        constraint_detail = f" sqlite_constraint={constraint_names}" if constraint_names else ""
                        LOGGER.error(
                            "Broker runtime stopped unexpectedly: %s%s%s",
                            failure_types,
                            sqlite_detail,
                            constraint_detail,
                            exc_info=(type(failure), failure, failure.__traceback__),
                        )
                    await bundle.close()
                    await asyncio.gather(task, return_exceptions=True)
                    self._bundles.pop(broker_id, None)
                    self._tasks.pop(broker_id, None)
                    bundle = None
                if bundle is None:
                    connection = await asyncio.to_thread(self._core.broker_connection, broker_id)
                    if connection.account_id and any(
                        command.account_id != connection.account_id for command in commands
                    ):
                        raise ValueError("Broker command account mismatch")
                    bundle = await self._builder(connection)
                    self._bundles[broker_id] = bundle
                    self._tasks[broker_id] = asyncio.create_task(bundle.runtime.run())
                await bundle.runtime.replace_commands(tuple(commands))
                published.add(broker_id)
            replacement_complete = True
        finally:
            if self._command_sync_diagnostics is not None:
                grouped_ids = {
                    str(command.automation_id) for broker_id in published for command in grouped.get(broker_id, ())
                }
                await self._capture_command_sync(
                    grouped_ids,
                    None if replacement_complete else "REPLACE_FAILED",
                )
