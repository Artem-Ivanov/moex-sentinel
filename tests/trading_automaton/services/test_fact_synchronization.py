"""Typed baseline Worker-to-Core synchronization behavior."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

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
from tests.contracts.test_trading_facts_contract import all_envelopes
from trading_automaton.domain.storage_dtos import FactOutboxRecord
from trading_automaton.services.fact_synchronization import FactSynchronizationService

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


@pytest.mark.parametrize("previous_retries", [10, 1024])
def test_long_outage_keeps_next_fact_retry_bounded_and_preserves_envelope(previous_retries):
    pending = row().model_copy(update={"retry_count": previous_retries})
    repository = RepositoryStub([pending])
    client = ClientStub(httpx.ConnectError("synthetic outage"))
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
        self.reconciled: list[tuple[str, str, int, int]] = []
        self.holds: list[tuple[str | None, str]] = []

    def cache_command(self, command: AutomationCommand) -> bool:
        self.cached.append(command)
        return True

    def ready_fact_outbox(self, limit: int, *, now: datetime, deadline_ms: int) -> list[FactOutboxRecord]:
        return self.rows[:limit]

    def acknowledge_fact_outbox(
        self,
        automation_id: str,
        *,
        accepted_through_sequence: int,
        current_revision: int,
    ) -> None:
        self.acknowledged.append((automation_id, accepted_through_sequence, current_revision))

    def schedule_fact_retry(
        self,
        event_ids: tuple[str, ...],
        *,
        retry_count: int,
        next_retry_at: datetime,
    ) -> None:
        self.retries.append((event_ids, retry_count, next_retry_at))

    def reject_fact_outbox(self, automation_id: str, event_ids: tuple[str, ...], *, reason: str) -> None:
        self.rejected.append((automation_id, event_ids, reason))

    def reconcile_from_core(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int,
    ) -> None:
        self.reconciled.append((automation_id, state, revision, last_sequence_number))

    def hold_active(self, reason: str, automation_id: str | None = None) -> None:
        self.holds.append((automation_id, reason))


class ClientStub:
    def __init__(self, *outcomes: FactBatchResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.published: list[list[FactEnvelope]] = []
        self.status_batches: list[tuple[UUID, ...]] = []

    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
        return []

    def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
        self.published.append(facts)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def automation_statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
        self.status_batches.append(tuple(automation_ids))
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
    assert repository.reconciled == [(failed.automation_id, "HOLD", 4, 7)]
    assert client.status_batches == [(UUID(failed.automation_id),)]


def test_retries_byte_equivalent_batch_on_transport_failure() -> None:
    pending = row(0)
    client = ClientStub(
        httpx.ConnectError("offline"),
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
