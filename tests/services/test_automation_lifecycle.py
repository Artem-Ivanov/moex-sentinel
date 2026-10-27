"""Public commands keep terminal states and idempotent revisions."""

import pytest

from moex_sentinel.services.automations import AutomationService, AutomationStateConflictError
from moex_sentinel.storage.models import TradingAutomationModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.repositories.automations import AutomationRepository
from sentinel_contracts.trading import AutomationState
from tests.storage.test_automation_command_repository import AUTOMATION_ID, repository


def test_repeated_resume_is_a_noop_after_entering_queue() -> None:
    _, factory = repository()
    service = AutomationService(AutomationRepository(factory), access_mode="TRADE")
    held = service.hold(AUTOMATION_ID, "USER_HOLD")
    first = service.resume(AUTOMATION_ID)

    repeated = service.resume(AUTOMATION_ID)

    assert held.revision == 2
    assert first.revision == 3
    assert repeated == first
    assert repeated.state is AutomationState.IN_QUEUE


def test_explicit_user_hold_supersedes_technical_hold_provenance() -> None:
    _, factory = repository()
    technical_reason = "Uncertain broker intent could not be reconciled unambiguously"
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.state = AutomationState.HOLD.value
        automation.suspended_from_state = AutomationState.IN_WORK.value
        automation.hold_reason = technical_reason
        automation.revision = 2
        automation.resume_requested = True
        session.add(
            AutomationEventModel(
                event_id="technical-hold-event",
                automation_id=AUTOMATION_ID,
                user_broker_id=automation.user_broker_id,
                sequence_number=1,
                expected_revision=1,
                fact_kind="AUTOMATION_STATE_CHANGED",
                safe_message=technical_reason,
                payload={
                    "state": "HOLD",
                    "suspended_from_state": "IN_WORK",
                    "hold_reason": technical_reason,
                },
                occurred_at=automation.updated_at,
                received_at=automation.updated_at,
            )
        )
    service = AutomationService(AutomationRepository(factory), access_mode="TRADE")
    user = service.hold(AUTOMATION_ID, technical_reason)

    assert user.state is AutomationState.HOLD
    assert user.suspended_from_state is AutomationState.IN_WORK
    assert user.revision == 3
    assert user.hold_reason == technical_reason
    assert user.resume_requested is False
    assert AutomationRepository(factory).is_current_worker_technical_hold(AUTOMATION_ID) is False


@pytest.mark.parametrize("command", ["hold", "close"])
def test_repeated_public_state_command_does_not_change_metadata(command: str) -> None:
    _, factory = repository()
    service = AutomationService(AutomationRepository(factory), access_mode="TRADE")
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
    service = AutomationService(AutomationRepository(factory), access_mode="TRADE")
    closed = service.close(AUTOMATION_ID)

    invoke = (
        (lambda: service.hold(AUTOMATION_ID, "USER_HOLD"))
        if command == "hold"
        else (lambda: service.resume(AUTOMATION_ID))
    )
    with pytest.raises(AutomationStateConflictError):
        invoke()

    assert service.get(AUTOMATION_ID) == closed
