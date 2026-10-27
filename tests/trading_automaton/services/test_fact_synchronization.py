"""Typed baseline Worker-to-Core synchronization behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatus,
    AutomationStatusesResult,
    FactBatchResult,
    FactEnvelope,
    FactGroupAcknowledgement,
    FactGroupFailure,
    FactIngressErrorCode,
)
from tests.contracts.trading_facts_helpers import all_envelopes
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import NOW as WORKER_NOW
from tests.trading_automaton.storage.worker_storage_helpers import baseline_command, filled_buy
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.domain.storage_dtos import FactOutboxRecord
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


@pytest.mark.parametrize("previous_retries", [10, 1024])
def test_long_outage_keeps_next_fact_retry_bounded_and_preserves_envelope(previous_retries):
    pending = row().model_copy(update={"retry_count": previous_retries})
    repository = RepositoryStub([pending])
    client = ClientStub(CoreOperationError(retryable=True))
    service = FactSynchronizationService(
        repository, client, now=lambda: NOW, sleep=lambda _: None, retry_limit=0, deadline_ms=0
    )
    assert service.flush_outbox() is False
    assert repository.retries == [((pending.event_id,), previous_retries + 1, NOW + timedelta(seconds=60))]
    assert str(client.published[0][0].event_id) == pending.event_id
    assert repository.acknowledged == repository.rejected == []


def row(index: int = 0) -> FactOutboxRecord:
    envelope = all_envelopes()[index]
    return FactOutboxRecord(
        event_id=str(envelope.event_id),
        user_broker_id=str(envelope.user_broker_id),
        automation_id=str(envelope.automation_id),
        sequence_number=envelope.sequence_number,
        expected_revision=envelope.expected_revision,
        fact_kind=envelope.fact_kind.value,
        payload=envelope.payload.model_dump(mode="json"),
        safe_message=envelope.safe_message,
        occurred_at=envelope.occurred_at,
        delivery_state="PENDING",
        retry_count=0,
        next_retry_at=None,
        created_at=envelope.occurred_at,
        updated_at=envelope.occurred_at,
    )


class RepositoryStub:
    def __init__(self, rows: list[FactOutboxRecord]) -> None:
        self.rows = rows
        self.cached: list[AutomationCommand] = []
        self.acknowledged: list[tuple[str, int, int]] = []
        self.retries: list[tuple[tuple[str, ...], int, datetime]] = []
        self.rejected: list[tuple[str, tuple[str, ...], str]] = []
        self.reconciled: list[tuple[str, str, int, int, int, Decimal | None]] = []
        self.holds: list[tuple[str | None, str]] = []
        self.recovery_requests: list[AutomationCommand] = []
        self.recovery_reconciliations: list[
            tuple[str, AutomationState | None, int | None, int | None, bool | None]
        ] = []
        self.recovery_rebase_permissions: list[bool] = []
        self.recovery_result: bool | None = None

    def cache_command(self, command: AutomationCommand) -> bool:
        self.cached.append(command)
        return True

    def consume_recovery_resume(self, command: AutomationCommand, *, occurred_at: datetime) -> AutomationCommand | None:
        self.recovery_requests.append(command)
        return command.model_copy(
            update={
                "state": AutomationState.IN_QUEUE,
                "revision": command.revision + 1,
                "last_sequence_number": command.last_sequence_number + 1,
            }
        )

    def reconcile_recovery_resume_outbox(
        self,
        automation_id: str,
        *,
        state: AutomationState | None,
        revision: int | None,
        last_sequence_number: int | None,
        resume_requested: bool | None,
        user_broker_id: str | None = None,
        allow_financial_rebase: bool = False,
    ) -> bool | None:
        self.recovery_reconciliations.append(
            (automation_id, state, revision, last_sequence_number, resume_requested)
        )
        self.recovery_rebase_permissions.append(allow_financial_rebase)
        return self.recovery_result

    def ready_fact_outbox(self, limit: int, *, now: datetime, deadline_ms: int) -> list[FactOutboxRecord]:
        return [row for row in self.rows if row.next_retry_at is None or row.next_retry_at <= now][:limit]

    def acknowledge_fact_outbox(
        self,
        automation_id: str,
        *,
        accepted_through_sequence: int,
        current_revision: int,
    ) -> None:
        self.acknowledged.append((automation_id, accepted_through_sequence, current_revision))
        self.rows = [
            row
            for row in self.rows
            if row.automation_id != automation_id or row.sequence_number > accepted_through_sequence
        ]

    def schedule_fact_retry(
        self,
        event_ids: tuple[str, ...],
        *,
        retry_count: int,
        next_retry_at: datetime,
    ) -> None:
        self.retries.append((event_ids, retry_count, next_retry_at))
        self.rows = [
            (
                row.model_copy(update={"retry_count": retry_count, "next_retry_at": next_retry_at})
                if row.event_id in event_ids
                else row
            )
            for row in self.rows
        ]

    def reject_fact_outbox(self, automation_id: str, event_ids: tuple[str, ...], *, reason: str) -> None:
        self.rejected.append((automation_id, event_ids, reason))

    def reconcile_from_core(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int,
        settings_revision: int,
        order_quota_percent: Decimal | None,
    ) -> None:
        self.reconciled.append(
            (automation_id, state, revision, last_sequence_number, settings_revision, order_quota_percent)
        )

    def hold_active(self, reason: str, automation_id: str | None = None) -> None:
        self.holds.append((automation_id, reason))


class ClientStub:
    def __init__(self, *outcomes: FactBatchResult | Exception, commands=(), statuses=None) -> None:
        self.outcomes = list(outcomes)
        self.commands = list(commands)
        self.statuses = statuses
        self.published: list[list[FactEnvelope]] = []
        self.status_batches: list[tuple[UUID, ...]] = []

    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
        return self.commands[:limit]

    def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
        self.published.append(facts)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def automation_statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
        self.status_batches.append(tuple(automation_ids))
        if self.statuses is not None:
            return self.statuses
        first = all_envelopes()[0]
        return AutomationStatusesResult(
            automations=(
                AutomationStatus(
                    automation_id=automation_ids[0],
                    user_broker_id=first.user_broker_id,
                    state=AutomationState.HOLD,
                    revision=4,
                    last_sequence_number=7,
                    resume_requested=False,
                    settings_revision=2,
                    order_quota_percent=Decimal("10"),
                ),
            )
        )


def test_acknowledges_success_and_schedules_only_retryable_failure() -> None:
    first = row(0)
    second = row(1).model_copy(update={"automation_id": str(UUID(int=702))})
    result = FactBatchResult(
        results=(
            FactGroupAcknowledgement(
                automation_id=UUID(first.automation_id),
                accepted_through_sequence=first.sequence_number,
                current_revision=2,
                accepted_event_ids=(UUID(first.event_id),),
            ),
        ),
        failures=(
            FactGroupFailure(
                automation_id=UUID(second.automation_id),
                code=FactIngressErrorCode.TEMPORARY_CORE_FAILURE,
                event_ids=(UUID(second.event_id),),
                sequence_numbers=(second.sequence_number,),
                retryable=True,
            ),
        ),
    )
    repository = RepositoryStub([first, second])
    service = FactSynchronizationService(
        repository,
        ClientStub(result),
        now=lambda: NOW,
        sleep=lambda _delay: None,
        deadline_ms=0,
    )

    assert service.flush_outbox() is True
    assert repository.acknowledged == [(first.automation_id, first.sequence_number, 2)]
    assert repository.retries == [((second.event_id,), 1, NOW + timedelta(seconds=1))]
    assert repository.reconciled == []


def test_non_retryable_failure_reconciles_without_acknowledging_failed_rows() -> None:
    failed = row(0)
    result = FactBatchResult(
        results=(),
        failures=(
            FactGroupFailure(
                automation_id=UUID(failed.automation_id),
                code=FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
                event_ids=(UUID(failed.event_id),),
                sequence_numbers=(failed.sequence_number,),
                retryable=False,
            ),
        ),
    )
    repository = RepositoryStub([failed])
    client = ClientStub(result)
    service = FactSynchronizationService(
        repository,
        client,
        now=lambda: NOW,
        sleep=lambda _delay: None,
        deadline_ms=0,
    )

    assert service.flush_outbox() is True
    assert repository.acknowledged == []
    assert repository.rejected == [
        (
            failed.automation_id,
            (failed.event_id,),
            FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT.value,
        )
    ]
    assert repository.reconciled == [(failed.automation_id, "HOLD", 4, 7, 2, Decimal("10"))]
    assert client.status_batches == [(UUID(failed.automation_id),)]
    assert repository.recovery_reconciliations == [
        (failed.automation_id, AutomationState.HOLD, 4, 7, False)
    ]
    assert repository.recovery_rebase_permissions == [True]


def test_non_revision_conflict_never_enables_financial_rebase() -> None:
    failed = row(0)
    result = FactBatchResult(
        results=(),
        failures=(
            FactGroupFailure(
                automation_id=UUID(failed.automation_id),
                code=FactIngressErrorCode.INVALID_FACT_STATE,
                event_ids=(UUID(failed.event_id),),
                sequence_numbers=(failed.sequence_number,),
                retryable=False,
            ),
        ),
    )
    repository = RepositoryStub([failed])
    service = FactSynchronizationService(
        repository,
        ClientStub(result),
        now=lambda: NOW,
        sleep=lambda _delay: None,
        deadline_ms=0,
    )

    assert service.flush_outbox() is True
    assert repository.recovery_rebase_permissions == [False]


def test_recovery_resume_claim_only_writes_queue_proposal_without_returning_activation_command() -> None:
    incoming = row(0)
    command = AutomationCommand.model_validate(
        {
            "automation_id": incoming.automation_id,
            "user_broker_id": incoming.user_broker_id,
            "broker_id": str(UUID(int=703)),
            "account_id": "account",
            "external_instrument_id": "instrument",
            "instrument_id": str(UUID(int=704)),
            "currency": "RUB",
            "lot_size": 1,
            "min_price_increment": "0.01",
            "state": "HOLD",
            "revision": 4,
            "last_sequence_number": 7,
            "resume_requested": True,
        }
    )
    repository = RepositoryStub([])
    service = FactSynchronizationService(
        repository,
        ClientStub(commands=[command]),
        now=lambda: NOW,
        sleep=lambda _delay: None,
    )

    claimed = service.claim_commands("worker", 10)

    assert claimed == []
    assert repository.cached == []
    assert repository.recovery_requests == [command]


def test_recovery_resume_cancellation_with_unknown_boundary_blocks_without_rejecting() -> None:
    failed = row(0)
    result = FactBatchResult(
        results=(),
        failures=(
            FactGroupFailure(
                automation_id=UUID(failed.automation_id),
                code=FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
                event_ids=(UUID(failed.event_id),),
                sequence_numbers=(failed.sequence_number,),
                retryable=False,
            ),
        )
    )
    repository = RepositoryStub([failed])
    repository.recovery_result = False
    client = ClientStub(result, statuses=AutomationStatusesResult(automations=()))
    service = FactSynchronizationService(
        repository,
        client,
        now=lambda: NOW,
        sleep=lambda _delay: None,
        deadline_ms=0,
    )

    assert service.flush_outbox() is False
    assert repository.rejected == []
    assert repository.holds == []
    assert repository.recovery_reconciliations == [(failed.automation_id, None, None, None, None)]


def test_revision_conflict_before_resume_claim_rebases_real_financial_graph(
    worker_repository_factory,
) -> None:
    repo, factory = worker_repository_factory()
    command = baseline_command()
    automation_id = str(command.automation_id)
    repo.cache_command(command)
    repo.save_decision_batch((decision_item(command),), occurred_at=WORKER_NOW)
    repo.finalize_execution(filled_buy())
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, automation_id)
        cached.state = AutomationState.HOLD.value
        cached.resume_requested = False
    pending = repo.ready_fact_outbox(100, now=WORKER_NOW + timedelta(minutes=1), deadline_ms=0)
    assert len(pending) >= 5
    result = FactBatchResult(
        results=(),
        failures=(
            FactGroupFailure(
                automation_id=command.automation_id,
                code=FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
                event_ids=tuple(UUID(item.event_id) for item in pending),
                sequence_numbers=tuple(item.sequence_number for item in pending),
                retryable=False,
            ),
        )
    )
    status = AutomationStatus(
        automation_id=command.automation_id,
        user_broker_id=command.user_broker_id,
        state=AutomationState.HOLD,
        revision=command.revision + 1,
        last_sequence_number=0,
        resume_requested=False,
    )
    client = ClientStub(result, statuses=AutomationStatusesResult(automations=(status,)))
    service = FactSynchronizationService(
        repo,
        client,
        now=lambda: WORKER_NOW + timedelta(minutes=1),
        sleep=lambda _delay: None,
        deadline_ms=0,
    )

    assert service.flush_outbox() is True
    rebased = repo.ready_fact_outbox(100, now=WORKER_NOW + timedelta(minutes=2), deadline_ms=0)
    assert [item.sequence_number for item in rebased] == list(range(1, len(pending) + 1))
    assert [item.event_id for item in rebased] == [item.event_id for item in pending]
    assert [item.payload for item in rebased] == [item.payload for item in pending]
    assert {item.expected_revision for item in rebased} == {status.revision}
    with factory() as session:
        cached = session.get_one(CachedAutomationModel, automation_id)
        assert cached.state == AutomationState.HOLD.value
        assert cached.resume_requested is False
        assert session.scalar(
            select(FactOutboxModel.event_id).where(
                FactOutboxModel.automation_id == automation_id,
                FactOutboxModel.safe_message == "Automation resumed after technical recovery",
            )
        ) is None


def test_retries_byte_equivalent_batch_on_transport_failure() -> None:
    pending = row(0)
    client = ClientStub(
        CoreOperationError(retryable=True),
        FactBatchResult(
            results=(
                FactGroupAcknowledgement(
                    automation_id=UUID(pending.automation_id),
                    accepted_through_sequence=pending.sequence_number,
                    current_revision=2,
                    accepted_event_ids=(UUID(pending.event_id),),
                ),
            )
        ),
    )
    repository = RepositoryStub([pending])
    delays: list[float] = []
    service = FactSynchronizationService(
        repository,
        client,
        now=lambda: NOW,
        sleep=delays.append,
        retry_limit=1,
        deadline_ms=0,
    )

    assert service.flush_outbox() is True
    assert client.published[0] == client.published[1]
    assert client.published[0][0] is client.published[1][0]
    assert delays == [1.0]


@pytest.mark.parametrize(
    "failure",
    [
        CoreOperationError(retryable=True),
        CoreOperationError(retryable=True, status_code=503),
    ],
    ids=["transport", "http-503"],
)
def test_durable_retry_starts_after_internal_attempts_and_blocks_until_due(failure: Exception) -> None:
    pending = row().model_copy(update={"retry_count": 2})
    clock = [NOW]
    repository = RepositoryStub([pending])
    client = ClientStub(failure, failure, FactBatchResult(results=()))

    def slow_sleep(delay: float) -> None:
        assert delay == 1.0
        clock[0] += timedelta(seconds=30)

    service = FactSynchronizationService(
        repository, client, now=lambda: clock[0], sleep=slow_sleep, retry_limit=1, deadline_ms=0
    )

    assert service.flush_outbox() is False
    due = NOW + timedelta(seconds=34)
    assert repository.retries == [((pending.event_id,), 3, due)]
    assert [batch[0].model_dump_json() for batch in client.published] == [client.published[0][0].model_dump_json()] * 2
    clock[0] = due - timedelta(microseconds=1)
    assert service.flush_outbox() is True
    assert len(client.published) == 2
    clock[0] = due
    assert service.flush_outbox() is True
    assert len(client.published) == 3
    assert client.published[2][0].model_dump_json() == client.published[0][0].model_dump_json()


def test_retryable_group_response_starts_retry_from_response_time_with_selective_ack() -> None:
    accepted = row(0)
    failed = row(1).model_copy(update={"automation_id": str(UUID(int=702))})
    result = FactBatchResult(
        results=(
            FactGroupAcknowledgement(
                automation_id=UUID(accepted.automation_id),
                accepted_through_sequence=accepted.sequence_number,
                current_revision=2,
                accepted_event_ids=(UUID(accepted.event_id),),
            ),
        ),
        failures=(
            FactGroupFailure(
                automation_id=UUID(failed.automation_id),
                code=FactIngressErrorCode.TEMPORARY_CORE_FAILURE,
                event_ids=(UUID(failed.event_id),),
                sequence_numbers=(failed.sequence_number,),
                retryable=True,
            ),
        ),
    )
    clock = [NOW]

    class SlowClient(ClientStub):
        def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
            clock[0] += timedelta(seconds=30)
            return super().publish_facts(facts)

    repository = RepositoryStub([accepted, failed])
    client = SlowClient(result, FactBatchResult(results=()))
    service = FactSynchronizationService(repository, client, now=lambda: clock[0], sleep=lambda _: None, deadline_ms=0)

    assert service.flush_outbox() is True
    due = NOW + timedelta(seconds=31)
    assert repository.acknowledged == [(accepted.automation_id, accepted.sequence_number, 2)]
    assert repository.retries == [((failed.event_id,), 1, due)]
    assert [row.event_id for row in repository.rows] == [failed.event_id]
    clock[0] = due - timedelta(microseconds=1)
    assert service.flush_outbox() is True
    assert len(client.published) == 1
    clock[0] = due
    assert service.flush_outbox() is True
    assert len(client.published) == 2
    assert client.published[1][0].model_dump_json() == client.published[0][1].model_dump_json()


def test_closed_outbox_scope_validation_does_not_require_live_command_connection():
    pending = row()
    repository = RepositoryStub([pending])
    client = ClientStub(FactBatchResult(results=()))
    validated = []

    def validate_commands(commands):
        pytest.fail("delivery requires ACTIVE command connection")

    def validate_facts(facts):
        validated.extend(str(fact.user_broker_id) for fact in facts)

    service = FactSynchronizationService(
        repository,
        client,
        now=lambda: NOW,
        sleep=lambda _: None,
        deadline_ms=0,
        validate_commands=validate_commands,
        validate_facts=validate_facts,
    )
    assert service.flush_outbox() is True
    assert validated == [pending.user_broker_id]
    assert len(client.published) == 1


def test_wrong_closed_outbox_scope_prevents_core_publication():
    pending = row()
    repository = RepositoryStub([pending])
    client = ClientStub()

    def validate_facts(facts):
        raise ValueError("Worker/Core environment mismatch")

    service = FactSynchronizationService(
        repository, client, now=lambda: NOW, sleep=lambda _: None, deadline_ms=0, validate_facts=validate_facts
    )
    with pytest.raises(ValueError, match="environment mismatch"):
        service.flush_outbox()
    assert client.published == []
    assert repository.acknowledged == repository.rejected == []


@pytest.mark.parametrize("status", [302, 400, 409])
def test_permanent_protocol_failure_reconciles_and_rejects_without_retry(status):
    pending = row()
    repository = RepositoryStub([pending])
    client = ClientStub(CoreOperationError(retryable=False, status_code=status))
    delays = []
    service = FactSynchronizationService(
        repository, client, now=lambda: NOW, sleep=delays.append, retry_limit=5, deadline_ms=0
    )
    assert service.flush_outbox() is True
    assert len(client.published) == 1
    assert client.status_batches == [(UUID(pending.automation_id),)]
    assert repository.rejected == [(pending.automation_id, (pending.event_id,), f"HTTP_{status}")]
    assert repository.reconciled == [(pending.automation_id, "HOLD", 4, 7, 2, Decimal("10"))]
    assert repository.retries == delays == []
