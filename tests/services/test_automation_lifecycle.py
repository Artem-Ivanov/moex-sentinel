"""Public commands keep terminal states and idempotent revisions."""

import pytest

from moex_sentinel.services.automations import AutomationService, AutomationStateConflictError
from moex_sentinel.storage.models import TradingAutomationModel
from moex_sentinel.storage.repositories.automations import AutomationRepository
from sentinel_contracts.trading import AutomationState
from tests.storage.test_automation_command_repository import AUTOMATION_ID, repository


def test_repeated_resume_is_a_noop_after_entering_queue() -> None:
    _, factory = repository()
    service = AutomationService(AutomationRepository(factory))
    held = service.hold(AUTOMATION_ID, "USER_HOLD")
    first = service.resume(AUTOMATION_ID)

    repeated = service.resume(AUTOMATION_ID)

    assert held.revision == 2
    assert first.revision == 3
    assert repeated == first
    assert repeated.state is AutomationState.IN_QUEUE


@pytest.mark.parametrize("command", ["hold", "close"])
def test_repeated_public_state_command_does_not_change_metadata(command: str) -> None:
    _, factory = repository()
    service = AutomationService(AutomationRepository(factory))
    invoke = (
        (lambda: service.hold(AUTOMATION_ID, "USER_HOLD"))
        if command == "hold"
        else (lambda: service.close(AUTOMATION_ID))
    )
    first = invoke()
    with factory() as session:
        before = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        metadata = before.updated_at, before.closed_at, before.suspended_from_state

    assert invoke() == first
    with factory() as session:
        after = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        assert (after.updated_at, after.closed_at, after.suspended_from_state) == metadata


@pytest.mark.parametrize("command", ["hold", "resume"])
def test_closed_automation_reports_existing_business_conflict(command: str) -> None:
    _, factory = repository()
    service = AutomationService(AutomationRepository(factory))
    closed = service.close(AUTOMATION_ID)

    invoke = (
        (lambda: service.hold(AUTOMATION_ID, "USER_HOLD"))
        if command == "hold"
        else (lambda: service.resume(AUTOMATION_ID))
    )
    with pytest.raises(AutomationStateConflictError):
        invoke()

    assert service.get(AUTOMATION_ID) == closed
