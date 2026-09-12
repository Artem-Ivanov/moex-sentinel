"""Coordinate one persistent streaming runtime per active broker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic as system_monotonic
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.core_contracts import AutomationStatusesResult
from trading_automaton.services.automation_lifecycle import WorkerAutomationLifecycleService

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


class CoordinatorRepositoryPort(Protocol):
    def transition_state(
        self,
        *,
        automation_id: str,
        state: str,
        safe_message: str,
        occurred_at: datetime,
    ) -> bool: ...

    def list_monitored(self) -> list[AutomationCommand]: ...

    def list_active(self) -> list[AutomationCommand]: ...

    def get_active_intent(self, automation_id: str) -> object | None: ...

    def synchronize_core_state(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int | None,
    ) -> AutomationCommand: ...

    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...


class CoordinatorSynchronizationPort(Protocol):
    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]: ...

    def flush_outbox(self) -> bool: ...


class CoordinatorCorePort(Protocol):
    def automation_statuses(self, automation_ids: list[str]) -> AutomationStatusesResult: ...

    def broker_connection(self, broker_id: str) -> BrokerConnection: ...

    def heartbeat(self, worker_id: str, occurred_at: datetime) -> None: ...


class RuntimePort(Protocol):
    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...


class RuntimeBundlePort(Protocol):
    broker_id: str
    runtime: RuntimePort

    async def close(self) -> None: ...


class StreamingRuntimeCoordinatorService:
    def __init__(
        self,
        repository: CoordinatorRepositoryPort,
        synchronization: CoordinatorSynchronizationPort,
        core: CoordinatorCorePort,
        builder: Callable[[BrokerConnection], Awaitable[RuntimeBundlePort]],
        *,
        worker_id: str,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = system_monotonic,
        heartbeat_interval_seconds: float = 3.0,
        command_limit: int = 100,
    ) -> None:
        self._repository = repository
        self._lifecycle = WorkerAutomationLifecycleService(repository)
        self._synchronization = synchronization
        self._core = core
        self._builder = builder
        self._worker_id = worker_id
        self._now = now
        self._monotonic = monotonic
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._next_heartbeat_at = 0.0
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._command_limit = command_limit
        self._bundles: dict[str, RuntimeBundlePort] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run_iteration(self) -> None:
        if not await self._flush_all():
            self._schedule_heartbeat()
            return
        claimed = await asyncio.to_thread(
            self._synchronization.claim_commands,
            self._worker_id,
            self._command_limit,
        )
        for command in claimed:
            if command.bootstrap is None:
                self._lifecycle.propose(
                    automation_id=str(command.automation_id),
                    target=AutomationState.IN_WORK,
                    safe_message="Automation claimed by streaming worker",
                    occurred_at=self._now(),
                )
        if not await self._flush_all():
            return
        monitored = await asyncio.to_thread(self._repository.list_monitored)
        core_states = await self._synchronize_all(monitored)
        active = await asyncio.to_thread(self._repository.list_active)
        monitored = await asyncio.to_thread(self._repository.list_monitored)
        active.extend(
            command for command in monitored if command.state is AutomationState.HOLD and command.bootstrap is not None
        )
        grouped: dict[str, list[AutomationCommand]] = {}
        for command in active:
            if command.bootstrap is not None:
                core_state = core_states.get(str(command.automation_id))
                if core_state == "CLOSED":
                    active_intent = await asyncio.to_thread(
                        self._repository.get_active_intent, str(command.automation_id)
                    )
                    if active_intent is None:
                        continue
                    command = command.model_copy(update={"state": AutomationState.CLOSED})
                elif core_state not in {"HOLD", "IN_QUEUE", "IN_WORK"}:
                    continue
                elif core_state in {"HOLD", "IN_QUEUE"}:
                    command = command.model_copy(update={"state": AutomationState(core_state)})
            grouped.setdefault(str(command.broker_id), []).append(command)
        await self._replace_broker_commands(grouped)
        self._schedule_heartbeat()

    async def close(self) -> None:
        await asyncio.gather(*(bundle.close() for bundle in self._bundles.values()))
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._heartbeat_task is not None:
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        self._bundles.clear()
        self._tasks.clear()

    def _schedule_heartbeat(self) -> None:
        current = self._monotonic()
        if current < self._next_heartbeat_at:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._next_heartbeat_at = current + self._heartbeat_interval_seconds
        occurred_at = self._now()
        self._heartbeat_task = asyncio.create_task(
            asyncio.to_thread(self._core.heartbeat, self._worker_id, occurred_at)
        )

    async def _flush_all(self) -> bool:
        return await asyncio.to_thread(self._synchronization.flush_outbox)

    async def _synchronize_all(self, commands: list[AutomationCommand]) -> dict[str, str]:
        if not commands:
            return {}
        result = await asyncio.to_thread(
            self._core.automation_statuses,
            [str(command.automation_id) for command in commands],
        )
        statuses = result.automations
        missing = set(result.missing_automation_ids)
        for command in commands:
            automation_id = str(command.automation_id)
            if automation_id in missing:
                await asyncio.to_thread(
                    self._repository.hold_active,
                    "AUTOMATION_MISSING_IN_CORE",
                    automation_id,
                )
                continue
            status = statuses[automation_id]
            await asyncio.to_thread(
                self._repository.synchronize_core_state,
                automation_id,
                state=str(status["state"]),
                revision=int(str(status["revision"])),
                last_sequence_number=(
                    int(str(status["last_sequence_number"])) if "last_sequence_number" in status else None
                ),
            )
        return {automation_id: str(status["state"]) for automation_id, status in statuses.items()}

    async def _replace_broker_commands(self, grouped: dict[str, list[AutomationCommand]]) -> None:
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
                bundle = await self._builder(connection)
                self._bundles[broker_id] = bundle
                self._tasks[broker_id] = asyncio.create_task(bundle.runtime.run())
            await bundle.runtime.replace_commands(tuple(commands))
