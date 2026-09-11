"""Worker lifecycle proposals persisted through the local transactional outbox."""

from datetime import datetime
from typing import Protocol

from sentinel_contracts.trading import AutomationState


class AutomationLifecycleRepositoryPort(Protocol):
    def transition_state(
        self,
        *,
        automation_id: str,
        state: str,
        safe_message: str,
        occurred_at: datetime,
    ) -> bool: ...


class WorkerAutomationLifecycleService:
    def __init__(self, repository: AutomationLifecycleRepositoryPort) -> None:
        self._repository = repository

    def propose(
        self,
        *,
        automation_id: str,
        target: AutomationState,
        safe_message: str,
        occurred_at: datetime,
    ) -> bool:
        return self._repository.transition_state(
            automation_id=automation_id,
            state=target.value,
            safe_message=safe_message,
            occurred_at=occurred_at,
        )
