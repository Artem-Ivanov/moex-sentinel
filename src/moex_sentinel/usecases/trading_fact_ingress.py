"""Thin use cases for the typed Worker-to-Core fact boundary."""

from typing import Protocol
from uuid import UUID

from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatusesResult,
    FactBatchResult,
    FactEnvelope,
)


class ClaimAutomationCommandsPort(Protocol):
    def claim(self, limit: int, *, broker_id: UUID | None = None) -> list[AutomationCommand]: ...


class AutomationStatusesPort(Protocol):
    def statuses(self, automation_ids: list[UUID], *, broker_id: UUID | None = None) -> AutomationStatusesResult: ...


class ClaimAutomationCommandsUsecase:
    def __init__(self, repository: ClaimAutomationCommandsPort) -> None:
        self._repository = repository

    def execute(self, limit: int, *, broker_id: UUID | None = None) -> list[AutomationCommand]:
        return (
            self._repository.claim(limit) if broker_id is None else self._repository.claim(limit, broker_id=broker_id)
        )


class ViewAutomationStatusesUsecase:
    def __init__(self, repository: AutomationStatusesPort) -> None:
        self._repository = repository

    def execute(self, automation_ids: list[UUID], *, broker_id: UUID | None = None) -> AutomationStatusesResult:
        return (
            self._repository.statuses(automation_ids)
            if broker_id is None
            else self._repository.statuses(automation_ids, broker_id=broker_id)
        )


class PublishTradingFactsUsecase:
    def __init__(self, service: TradingFactIngressService) -> None:
        self._service = service

    def execute(
        self,
        facts: list[FactEnvelope],
        *,
        runtime_instance_id: UUID | None = None,
        generation: int | None = None,
    ) -> FactBatchResult:
        return self._service.publish(facts, runtime_instance_id=runtime_instance_id, generation=generation)
