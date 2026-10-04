"""Finite command synchronization operation for the streaming Worker."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.core_contracts import AutomationStatusesResult
from trading_automaton.services.automation_lifecycle import WorkerAutomationLifecycleService


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


class CoordinatorStatusPort(Protocol):
    def automation_statuses(self, automation_ids: list[str]) -> AutomationStatusesResult: ...


class SynchronizeTradingRuntimeUsecase:
    """Own command reconciliation without owning runtime tasks or heartbeat timers."""

    def __init__(
        self,
        repository: CoordinatorRepositoryPort,
        synchronization: CoordinatorSynchronizationPort,
        core: CoordinatorStatusPort,
        lifecycle: WorkerAutomationLifecycleService,
        *,
        worker_id: str,
        now: Callable[[], datetime],
        command_limit: int = 100,
    ) -> None:
        self._repository = repository
        self._synchronization = synchronization
        self._core = core
        self._lifecycle = lifecycle
        self._worker_id = worker_id
        self._now = now
        self._command_limit = command_limit

    async def execute(
        self,
        *,
        replace_broker_commands: Callable[[dict[str, list[AutomationCommand]]], Awaitable[None]],
    ) -> Literal["COMPLETED", "OUTBOX_BLOCKED"]:
        """Flush, claim and reconcile commands before publishing the current broker groups."""
        if not await self._flush_all():
            return "OUTBOX_BLOCKED"
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
            return "OUTBOX_BLOCKED"
        monitored = await asyncio.to_thread(self._repository.list_monitored)
        core_states = await self._synchronize_all(monitored)
        active = await asyncio.to_thread(self._repository.list_active)
        monitored = await asyncio.to_thread(self._repository.list_monitored)
        for command in monitored:
            if command.state is not AutomationState.HOLD:
                continue
            active_intent = await asyncio.to_thread(self._repository.get_active_intent, str(command.automation_id))
            if command.bootstrap is not None or active_intent is not None:
                # Preserve HOLD: supervise uncertain execution without enabling decisions.
                active.append(command)
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
        await replace_broker_commands(grouped)
        return "COMPLETED"

    async def _flush_all(self) -> bool:
        """Flush durable facts before progressing command reconciliation."""
        return await asyncio.to_thread(self._synchronization.flush_outbox)

    async def _synchronize_all(self, commands: list[AutomationCommand]) -> dict[str, str]:
        """Apply Core states to monitored commands, holding automations missing from Core."""
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
