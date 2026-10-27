from uuid import UUID, uuid4

from moex_sentinel.usecases.trading_fact_ingress import (
    ClaimAutomationCommandsUsecase,
    ViewAutomationStatusesUsecase,
)
from sentinel_contracts.trading_facts import AutomationCommand, AutomationStatusesResult


def test_claim_usecase_needs_only_claim_capability_and_returns_repository_result():
    expected: list[AutomationCommand] = []

    class ClaimOnly:
        def claim(self, limit: int) -> list[AutomationCommand]:
            assert limit == 7
            return expected

    assert ClaimAutomationCommandsUsecase(ClaimOnly()).execute(7) is expected


def test_status_usecase_passes_ids_and_returns_repository_result():
    ids = [uuid4()]
    expected = AutomationStatusesResult(automations=())

    class StatusesOnly:
        def statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
            assert automation_ids is ids
            return expected

    assert ViewAutomationStatusesUsecase(StatusesOnly()).execute(ids) is expected
