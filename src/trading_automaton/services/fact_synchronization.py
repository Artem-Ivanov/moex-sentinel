"""Reliable batch delivery for the typed Worker-to-Core fact protocol."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import TypeAdapter

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatusesResult,
    FactBatchResult,
    FactEnvelope,
    FactGroupFailure,
    FactIngressErrorCode,
)
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.domain.storage_dtos import FactOutboxRecord

_FACT_ENVELOPE_ADAPTER: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)


class FactRepositoryPort(Protocol):
    def cache_command(self, command: AutomationCommand) -> bool: ...

    def consume_recovery_resume(
        self,
        command: AutomationCommand,
        *,
        occurred_at: datetime,
    ) -> AutomationCommand | None: ...

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
    ) -> bool | None: ...

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
        state: AutomationState,
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
        validate_commands: Callable[[list[AutomationCommand]], None] | None = None,
        validate_facts: Callable[[list[FactEnvelope]], None] | None = None,
    ) -> None:
        self._validate_facts = validate_facts
        self._validate_commands = validate_commands
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
        if self._validate_commands is not None:
            self._validate_commands(commands)
        claimed: list[AutomationCommand] = []
        for command in commands:
            if command.state is AutomationState.HOLD and command.resume_requested:
                self._repository.consume_recovery_resume(command, occurred_at=self._now())
                continue
            if self._repository.cache_command(command):
                claimed.append(command)
        return claimed

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
        if self._validate_facts is not None:
            self._validate_facts(facts)
        attempt = 0
        while True:
            try:
                result = self._client.publish_facts(facts)
            except CoreOperationError as error:
                if not error.retryable:
                    automation_ids = tuple(dict.fromkeys(UUID(row.automation_id) for row in rows))
                    return self._handle_nonretryable(
                        automation_ids,
                        {automation_id: tuple(row.event_id for row in rows if row.automation_id == str(automation_id))
                         for automation_id in automation_ids},
                        reason=f"HTTP_{error.status_code}",
                    )
                if attempt >= self._retry_limit:
                    self._schedule_rows(rows)
                    return False
            else:
                return self._apply_result(result, rows)
            self._sleep(2**attempt + self._jitter(attempt))
            attempt += 1

    def _apply_result(
        self,
        result: FactBatchResult,
        rows: list[FactOutboxRecord],
    ) -> bool:
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
                self._schedule_rows(failed_rows)
            else:
                non_retryable.append(failure)
        if not non_retryable:
            return True
        return self._handle_nonretryable(
            tuple(dict.fromkeys(failure.automation_id for failure in non_retryable)),
            {failure.automation_id: tuple(str(event_id) for event_id in failure.event_ids)
             for failure in non_retryable},
            reason_by_automation={failure.automation_id: failure.code.value for failure in non_retryable},
        )

    def _handle_nonretryable(
        self,
        automation_ids: tuple[UUID, ...],
        event_ids_by_automation: dict[UUID, tuple[str, ...]],
        *,
        reason: str | None = None,
        reason_by_automation: dict[UUID, str] | None = None,
    ) -> bool:
        result = self._client.automation_statuses(list(automation_ids))
        statuses = {status.automation_id: status for status in result.automations}
        missing = set(result.missing_automation_ids)
        fail_closed = False
        for automation_id in automation_ids:
            status = statuses.get(automation_id)
            handled = self._repository.reconcile_recovery_resume_outbox(
                str(automation_id),
                state=None if status is None else status.state,
                revision=None if status is None else status.revision,
                last_sequence_number=None if status is None else status.last_sequence_number,
                resume_requested=None if status is None else status.resume_requested,
                user_broker_id=None if status is None else str(status.user_broker_id),
                allow_financial_rebase=(
                    status is not None
                    and status.state is AutomationState.HOLD
                    and status.resume_requested is False
                    and (reason_by_automation or {}).get(automation_id)
                    == FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT.value
                ),
            )
            if handled is True:
                continue
            if handled is False:
                # The resume context is known but Core's safe cancellation boundary or suffix is not.
                fail_closed = True
                continue
            if status is None:
                reason_text = (
                    "Automation missing in Core"
                    if automation_id in missing or automation_id not in statuses
                    else "Typed fact state cannot be reconciled"
                )
                self._repository.hold_active(reason_text, str(automation_id))
            else:
                self._repository.reconcile_from_core(
                    str(automation_id),
                    state=status.state.value,
                    revision=status.revision,
                    last_sequence_number=status.last_sequence_number,
                    settings_revision=status.settings_revision,
                    order_quota_percent=status.order_quota_percent,
                )
            failure_reason = reason if reason is not None else (reason_by_automation or {}).get(
                automation_id, "CORE_REJECTED"
            )
            self._repository.reject_fact_outbox(
                str(automation_id),
                event_ids_by_automation[automation_id],
                reason=failure_reason,
            )
        return not fail_closed

    def _schedule_rows(self, rows: list[FactOutboxRecord]) -> None:
        if not rows:
            return
        retry_count = max(row.retry_count for row in rows) + 1
        self._repository.schedule_fact_retry(
            tuple(row.event_id for row in rows),
            retry_count=retry_count,
            next_retry_at=self._now() + timedelta(seconds=min(60, 2 ** min(retry_count - 1, 6))),
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
