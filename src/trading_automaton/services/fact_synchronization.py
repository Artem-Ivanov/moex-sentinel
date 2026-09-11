"""Reliable batch delivery for the typed Worker-to-Core fact protocol."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatusesResult,
    FactBatchResult,
    FactEnvelope,
    FactGroupFailure,
)
from trading_automaton.domain.storage_dtos import FactOutboxRecord

_FACT_ENVELOPE_ADAPTER: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)


class FactRepositoryPort(Protocol):
    def cache_command(self, command: AutomationCommand) -> bool: ...

    def ready_fact_outbox(
        self,
        limit: int,
        *,
        now: datetime,
        deadline_ms: int,
    ) -> list[FactOutboxRecord]: ...

    def acknowledge_fact_outbox(
        self,
        automation_id: str,
        *,
        accepted_through_sequence: int,
        current_revision: int,
    ) -> None: ...

    def schedule_fact_retry(
        self,
        event_ids: tuple[str, ...],
        *,
        retry_count: int,
        next_retry_at: datetime,
    ) -> None: ...

    def reject_fact_outbox(self, automation_id: str, event_ids: tuple[str, ...], *, reason: str) -> None: ...

    def reconcile_from_core(
        self,
        automation_id: str,
        *,
        state: str,
        revision: int,
        last_sequence_number: int,
    ) -> None: ...

    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...


class FactCoreClientPort(Protocol):
    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]: ...

    def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult: ...

    def automation_statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult: ...


class FactSynchronizationService:
    def __init__(
        self,
        repository: FactRepositoryPort,
        client: FactCoreClientPort,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        jitter: Callable[[int], float] = lambda _attempt: 0.0,
        retry_limit: int = 5,
        batch_size: int = 100,
        deadline_ms: int = 1000,
    ) -> None:
        self._repository = repository
        self._client = client
        self._now = now
        self._sleep = sleep
        self._jitter = jitter
        self._retry_limit = retry_limit
        self._batch_size = batch_size
        self._deadline_ms = deadline_ms

    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
        commands = self._client.claim_commands(worker_id, limit)
        return [command for command in commands if self._repository.cache_command(command)]

    def flush_outbox(self) -> bool:
        now = self._now()
        rows = self._repository.ready_fact_outbox(
            self._batch_size,
            now=now,
            deadline_ms=self._deadline_ms,
        )
        if not rows:
            return True
        facts = [self._envelope(row) for row in rows]
        attempt = 0
        while True:
            try:
                result = self._client.publish_facts(facts)
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500:
                    automation_ids = tuple(dict.fromkeys(UUID(row.automation_id) for row in rows))
                    self._reconcile(automation_ids)
                    for automation_id in automation_ids:
                        self._repository.reject_fact_outbox(
                            str(automation_id),
                            tuple(row.event_id for row in rows if row.automation_id == str(automation_id)),
                            reason=f"HTTP_{error.response.status_code}",
                        )
                    return True
                if attempt >= self._retry_limit:
                    self._schedule_rows(rows, now)
                    return False
            except httpx.TransportError:
                if attempt >= self._retry_limit:
                    self._schedule_rows(rows, now)
                    return False
            else:
                self._apply_result(result, rows, now)
                return True
            self._sleep(2**attempt + self._jitter(attempt))
            attempt += 1

    def _apply_result(
        self,
        result: FactBatchResult,
        rows: list[FactOutboxRecord],
        now: datetime,
    ) -> None:
        by_event_id = {row.event_id: row for row in rows}
        for acknowledgement in result.results:
            self._repository.acknowledge_fact_outbox(
                str(acknowledgement.automation_id),
                accepted_through_sequence=acknowledgement.accepted_through_sequence,
                current_revision=acknowledgement.current_revision,
            )
        non_retryable: list[FactGroupFailure] = []
        for failure in result.failures:
            if failure.retryable:
                failed_rows = [by_event_id[str(event_id)] for event_id in failure.event_ids]
                self._schedule_rows(failed_rows, now)
            else:
                non_retryable.append(failure)
        self._reconcile(tuple(dict.fromkeys(failure.automation_id for failure in non_retryable)))
        for failure in non_retryable:
            self._repository.reject_fact_outbox(
                str(failure.automation_id),
                tuple(str(event_id) for event_id in failure.event_ids),
                reason=failure.code.value,
            )

    def _reconcile(self, automation_ids: tuple[UUID, ...]) -> None:
        if not automation_ids:
            return
        result = self._client.automation_statuses(list(automation_ids))
        statuses = {status.automation_id: status for status in result.automations}
        missing = set(result.missing_automation_ids)
        for automation_id in automation_ids:
            status = statuses.get(automation_id)
            if status is None:
                reason = (
                    "Automation missing in Core"
                    if automation_id in missing or automation_id not in statuses
                    else "Typed fact state cannot be reconciled"
                )
                self._repository.hold_active(reason, str(automation_id))
                continue
            self._repository.reconcile_from_core(
                str(automation_id),
                state=status.state.value,
                revision=status.revision,
                last_sequence_number=status.last_sequence_number,
            )

    def _schedule_rows(self, rows: list[FactOutboxRecord], now: datetime) -> None:
        if not rows:
            return
        retry_count = max(row.retry_count for row in rows) + 1
        self._repository.schedule_fact_retry(
            tuple(row.event_id for row in rows),
            retry_count=retry_count,
            next_retry_at=now + timedelta(seconds=min(60, 2 ** min(retry_count - 1, 6))),
        )

    @staticmethod
    def _envelope(row: FactOutboxRecord) -> FactEnvelope:
        return _FACT_ENVELOPE_ADAPTER.validate_python(
            {
                "event_id": row.event_id,
                "user_broker_id": row.user_broker_id,
                "automation_id": row.automation_id,
                "sequence_number": row.sequence_number,
                "expected_revision": row.expected_revision,
                "fact_kind": row.fact_kind,
                "payload": row.payload,
                "safe_message": row.safe_message,
                "occurred_at": row.occurred_at,
            }
        )
