"""Business lifecycle for baseline trading automations owned by Core."""

from typing import Protocol

from moex_sentinel.domain.repository_records import AutomationRecord
from sentinel_contracts.automation_lifecycle import (
    InvalidAutomationTransition,
    TransitionOrigin,
    validate_automation_transition,
)
from sentinel_contracts.trading import AutomationState


class AutomationStateConflictError(ValueError):
    pass


class AutomationRepositoryPort(Protocol):
    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord: ...
    def get(self, automation_id: str) -> AutomationRecord: ...
    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]: ...
    def list_active(self) -> list[AutomationRecord]: ...
    def set_state(
        self,
        automation_id: str,
        state: AutomationState,
        *,
        expected_revision: int,
        hold_reason: str | None = None,
    ) -> AutomationRecord: ...
    def resume_to_queue(self, automation_id: str, *, expected_revision: int) -> AutomationRecord: ...


class AutomationService:
    def __init__(self, repository: AutomationRepositoryPort) -> None:
        self._repository = repository

    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord:
        return self._repository.create(broker_id=broker_id, account_id=account_id, instrument_id=instrument_id)

    def get(self, automation_id: str) -> AutomationRecord:
        return self._repository.get(automation_id)

    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]:
        return self._repository.get_many(automation_ids)

    def list_active(self) -> list[AutomationRecord]:
        return self._repository.list_active()

    def hold(self, automation_id: str, reason: str) -> AutomationRecord:
        current = self._repository.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.HOLD):
            return current
        return self._repository.set_state(
            automation_id,
            AutomationState.HOLD,
            expected_revision=current.revision,
            hold_reason=reason,
        )

    def resume(self, automation_id: str) -> AutomationRecord:
        current = self._repository.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.IN_QUEUE):
            return current
        return self._repository.resume_to_queue(automation_id, expected_revision=current.revision)

    def close(self, automation_id: str) -> AutomationRecord:
        current = self._repository.get(automation_id)
        if not self._validate_transition(current.state, AutomationState.CLOSED):
            return current
        return self._repository.set_state(
            automation_id,
            AutomationState.CLOSED,
            expected_revision=current.revision,
        )

    @staticmethod
    def _validate_transition(current: AutomationState, target: AutomationState) -> bool:
        try:
            return validate_automation_transition(current, target, origin=TransitionOrigin.USER_COMMAND)
        except InvalidAutomationTransition as error:
            raise AutomationStateConflictError(str(error)) from error
