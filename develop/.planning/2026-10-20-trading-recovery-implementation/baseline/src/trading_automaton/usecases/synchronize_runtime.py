"""Finite command synchronization operation for the streaming Worker."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.core_contracts import AutomationStatusesResult
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.services.automation_lifecycle import (
    WorkerAutomationLifecycleService,
)


class CoordinatorRepositoryPort(Protocol):
    def transition_state(
        self,
        *,
        automation_id: str,
        state: AutomationState,
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
        state: AutomationState,
        revision: int,
        last_sequence_number: int | None,
        settings_revision: int,
        order_quota_percent: Decimal | None,
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
        self.last_claimed_count = 0

    async def execute(
        self,
        *,
        replace_broker_commands: Callable[[dict[str, list[AutomationCommand]]], Awaitable[None]],
    ) -> Literal["COMPLETED", "OUTBOX_BLOCKED"]:
        """Flush, claim and reconcile commands before publishing the current broker groups."""
        self.last_claimed_count = 0
        try:
            if not await self._flush_all():
                await self._publish_transient_holds(replace_broker_commands)
                return "OUTBOX_BLOCKED"
            claimed = await asyncio.to_thread(
                self._synchronization.claim_commands,
                self._worker_id,
                self._command_limit,
            )
            self.last_claimed_count = len(claimed)
            for command in claimed:
                if command.bootstrap is None:
                    self._lifecycle.propose(
                        automation_id=str(command.automation_id),
                        target=AutomationState.IN_WORK,
                        safe_message="Automation claimed by streaming worker",
                        occurred_at=self._now(),
                    )
            if not await self._flush_all():
                await self._publish_transient_holds(replace_broker_commands)
                return "OUTBOX_BLOCKED"
            monitored = await asyncio.to_thread(self._repository.list_monitored)
            core_states = await self._synchronize_all(monitored)
        except CoreOperationError:
            # Preserve recovery while preventing decisions with stale per-automation settings.
            await self._publish_transient_holds(replace_broker_commands)
            raise
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

    @staticmethod
    def _group(commands: list[AutomationCommand]) -> dict[str, list[AutomationCommand]]:
        grouped: dict[str, list[AutomationCommand]] = {}
        for command in commands:
            grouped.setdefault(str(command.broker_id), []).append(command)
        return grouped

    async def _flush_all(self) -> bool:
        """Flush durable facts before progressing command reconciliation."""
        return await asyncio.to_thread(self._synchronization.flush_outbox)

    async def _publish_transient_holds(
        self,
        replace_broker_commands: Callable[[dict[str, list[AutomationCommand]]], Awaitable[None]],
    ) -> None:
        """Stop new decisions without persisting HOLD or interrupting intent recovery."""
        monitored = await asyncio.to_thread(self._repository.list_monitored)
        safe_commands = [
            (
                command.model_copy(update={"state": AutomationState.HOLD})
                if command.state is AutomationState.IN_WORK
                else command
            )
            for command in monitored
        ]
        await replace_broker_commands(self._group(safe_commands))

    async def _synchronize_all(self, commands: list[AutomationCommand]) -> dict[str, str]:
        """Apply Core states to monitored commands, holding automations missing from Core."""
        if not commands:
            return {}
        result = await asyncio.to_thread(
            self._core.automation_statuses,
            [str(command.automation_id) for command in commands],
        )
        statuses = result.automations
        missing = {str(automation_id) for automation_id in result.missing_automation_ids}
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
            raw_state = status["state"]
            await asyncio.to_thread(
                self._repository.synchronize_core_state,
                automation_id,
                state=(raw_state.value if isinstance(raw_state, AutomationState) else str(raw_state)),
                revision=int(str(status["revision"])),
                last_sequence_number=(
                    int(str(status["last_sequence_number"])) if status.get("last_sequence_number") is not None else None
                ),
                settings_revision=int(str(status.get("settings_revision", 0))),
                order_quota_percent=status.get("order_quota_percent"),
            )
        result: dict[str, str] = {}
        for automation_id, status in statuses.items():
            state = status["state"]
            result[automation_id] = state.value if isinstance(state, AutomationState) else str(state)
        return result
