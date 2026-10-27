"""Safe volatile control progress and Worker-owned outbox observations."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sentinel_contracts.broker_execution import BrokerOrderStatus, OrderSide
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading import AutomationState

Count = Annotated[int, Field(ge=0, strict=True)]
IterationResult = Literal["COMPLETED", "OUTBOX_BLOCKED", "ERROR"]
DiagnosticAmount = Annotated[Decimal, Field(ge=0, max_digits=28, decimal_places=9)]


class BrokerRuntimeDiagnostics(BaseModel):
    """Volatile broker iteration state, independent of command/control progress."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["IDLE", "RUNNING", "BLOCKED", "UNKNOWN"] = "UNKNOWN"
    decision_eligibility: Literal["READY", "NOT_READY", "UNKNOWN"] = "UNKNOWN"
    reason: Literal[
        "NOT_OBSERVED",
        "NO_ACTIVE_COMMAND",
        "ITERATION_RUNNING",
        "DECISION_FRAME_AVAILABLE",
        "NO_DECISION_FRAME",
        "BROKER_RETRYING",
        "PERMANENT_BROKER_ERROR",
        "STALE",
    ] = "NOT_OBSERVED"
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None

    @field_validator("last_started_at", "last_finished_at", "last_success_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        values = (self.last_started_at, self.last_finished_at, self.last_success_at)
        if any(value is not None and (value.tzinfo is None or value.utcoffset() is None) for value in values):
            raise ValueError("Broker observation times must be timezone aware")
        if self.status == "IDLE" and self.reason != "NO_ACTIVE_COMMAND":
            raise ValueError("Idle broker runtime requires no active command")
        if self.status == "IDLE" and self.decision_eligibility != "UNKNOWN":
            raise ValueError("Idle broker runtime cannot claim decision eligibility")
        if self.status == "BLOCKED" and (
            self.reason != "PERMANENT_BROKER_ERROR" or self.decision_eligibility != "UNKNOWN"
        ):
            raise ValueError("Blocked broker runtime requires a permanent broker error")
        if self.status == "UNKNOWN" and self.decision_eligibility != "UNKNOWN":
            raise ValueError("Unknown broker runtime cannot claim decision eligibility")
        if self.last_finished_at is not None and self.last_started_at is None:
            raise ValueError("Finished iteration requires a start time")
        if (
            self.last_success_at is not None
            and self.last_finished_at is not None
            and self.last_success_at > self.last_finished_at
        ):
            raise ValueError("Last success cannot follow the latest iteration finish")
        if (self.decision_eligibility == "READY") != (self.reason == "DECISION_FRAME_AVAILABLE"):
            raise ValueError("Decision eligibility requires an available decision frame")
        if self.reason == "DECISION_FRAME_AVAILABLE" and (
            self.status != "RUNNING" or self.last_finished_at is None or self.last_success_at is None
        ):
            raise ValueError("Decision-ready frame requires a completed successful iteration")
        if self.reason == "NO_DECISION_FRAME" and (
            self.status != "RUNNING" or self.decision_eligibility != "NOT_READY"
        ):
            raise ValueError("Unavailable decision frame requires an active runtime without eligibility")
        return self


class OutboxDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    reason: Literal["OBSERVED", "READ_FAILED"]
    pending_count: Count | None = None
    failed_count: Count | None = None
    oldest_pending_at: datetime | None = None
    max_retry_count: Count | None = None

    @field_validator("oldest_pending_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.observation == "UNKNOWN":
            if self.reason != "READ_FAILED" or any(
                value is not None
                for value in (
                    self.pending_count,
                    self.failed_count,
                    self.oldest_pending_at,
                    self.max_retry_count,
                )
            ):
                raise ValueError("Unknown queue values must be null")
        elif self.reason != "OBSERVED" or self.pending_count is None or self.failed_count is None:
            raise ValueError("Observed queue requires counts")
        elif self.pending_count == 0:
            if self.oldest_pending_at is not None or self.max_retry_count is not None:
                raise ValueError("Empty pending queue has no oldest time or retry count")
        elif self.oldest_pending_at is None or self.max_retry_count is None:
            raise ValueError("Pending queue requires oldest time and retry count")
        return self


class CommandSyncRow(BaseModel):
    """One safe cached automation row in a bounded Worker command projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    automation_id: UUID
    state: AutomationState
    revision: Count
    last_sequence_number: Count
    binding_complete: bool
    bootstrap: bool
    active_intent_state: BrokerOrderStatus | None
    active_intent_terminal_present: bool | None
    pending_fact_count: Count
    failed_fact_count: Count
    monitored: bool
    grouped: bool
    exclusion_reason: (
        Literal[
            "INCOMPLETE_BINDING",
            "NOT_MONITORED",
            "HOLD_WITHOUT_ACTIVE_INTENT_OR_BOOTSTRAP",
            "CLOSED_WITHOUT_ACTIVE_INTENT",
            "BOOTSTRAP_STATE_NOT_GROUPABLE",
            "PUBLICATION_UNKNOWN",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.active_intent_state is None and self.active_intent_terminal_present is not None:
            raise ValueError("Absent active intent cannot report terminal metadata")
        if self.grouped and (not self.monitored or not self.binding_complete):
            raise ValueError("Grouped automation must be monitored with complete bindings")
        if self.grouped and self.exclusion_reason is not None:
            raise ValueError("Grouped automation cannot have an exclusion reason")
        if not self.grouped and self.exclusion_reason is None:
            raise ValueError("Excluded automation requires a fixed reason")
        return self


class CommandSyncDiagnostics(BaseModel):
    """Bounded, read-only account of command discovery and publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    reason: Literal["OBSERVED", "UNCONFIGURED_BROKER", "UNSCOPED_CACHE", "READ_FAILED"] = "READ_FAILED"
    broker_id: UUID | None = None
    captured_at: datetime | None = None
    complete: bool = False
    truncated: bool = False
    cached_count: Count | None = None
    claimed_count: Count | None = None
    monitored_count: Count | None = None
    grouped_count: Count | None = None
    publication_observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    publication_reason: Literal["REPLACE_FAILED", "SYNC_FAILED"] | None = None
    rows: tuple[CommandSyncRow, ...] = Field(default=(), max_length=100)

    @field_validator("captured_at")
    @classmethod
    def normalize_captured_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.observation == "UNKNOWN":
            if (
                self.reason == "OBSERVED"
                or self.complete
                or any(
                    value is not None
                    for value in (
                        self.cached_count,
                        self.claimed_count,
                        self.monitored_count,
                        self.grouped_count,
                    )
                )
            ):
                raise ValueError("Unknown command projection cannot claim complete scoped counts")
            return self
        if self.reason != "OBSERVED" or self.broker_id is None or self.captured_at is None:
            raise ValueError("Observed command projection requires scope and capture time")
        if (
            self.observation == "OBSERVED"
            and self.publication_observation == "UNKNOWN"
            and self.publication_reason is None
        ):
            raise ValueError("Unknown publication state requires a fixed reason")
        if self.publication_observation == "OBSERVED" and self.publication_reason is not None:
            raise ValueError("Observed publication cannot have a failure reason")
        if self.truncated and self.complete:
            raise ValueError("Truncated command projection cannot be complete")
        if self.complete and (
            self.cached_count != len(self.rows)
            or self.monitored_count != sum(row.monitored for row in self.rows)
            or self.grouped_count != sum(row.grouped for row in self.rows)
            or any(not row.binding_complete for row in self.rows)
            or self.publication_observation != "OBSERVED"
        ):
            raise ValueError("Complete command projection counts or bindings disagree")
        return self


class WorkerIntentDiagnostics(BaseModel):
    """One redacted local intent row, with no strategy or transport payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: UUID
    decision_id: UUID | None
    process_id: UUID | None
    active: bool
    state: BrokerOrderStatus
    side: OrderSide
    external_order_id: str | None = Field(default=None, max_length=128)
    quantity_lots: Annotated[int, Field(gt=0, strict=True)]
    limit_price: DiagnosticAmount
    requested_amount: DiagnosticAmount
    executed_lots: Count
    executed_amount: DiagnosticAmount
    executed_price: DiagnosticAmount
    estimated_commission: DiagnosticAmount
    executed_commission: DiagnosticAmount
    currency: str | None = Field(default=None, max_length=8)
    fact_execution_id: UUID | None
    execution_facts_emitted_at: datetime | None
    origin_runtime_instance_id: UUID | None
    origin_generation: Annotated[int, Field(gt=0, strict=True)] | None
    created_at: datetime
    updated_at: datetime
    dispatch_started_at: datetime | None
    broker_responded_at: datetime | None
    executed_at: datetime | None
    terminal_at: datetime | None

    @field_validator(
        "created_at",
        "updated_at",
        "dispatch_started_at",
        "broker_responded_at",
        "executed_at",
        "terminal_at",
        "execution_facts_emitted_at",
    )
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent_origin(self) -> Self:
        if (self.origin_runtime_instance_id is None) != (self.origin_generation is None):
            raise ValueError("Intent origin requires both runtime and generation")
        return self


class WorkerAutomationExecutionDiagnostics(BaseModel):
    """Bounded intent history and immutable automation bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    automation_id: UUID
    account_id: str = Field(min_length=1, max_length=128)
    external_instrument_id: str = Field(min_length=1, max_length=128)
    fact_instrument_id: UUID | None
    lot_size: Annotated[int, Field(gt=0, strict=True)]
    pending_fact_count: Count
    failed_fact_count: Count
    intents: tuple[WorkerIntentDiagnostics, ...] = Field(default=(), max_length=5)
    history_truncated: bool = False

    @model_validator(mode="after")
    def active_first(self) -> Self:
        active = tuple(intent.active for intent in self.intents)
        if active != tuple(sorted(active, reverse=True)):
            raise ValueError("Active intents must precede terminal history")
        return self


class WorkerExecutionDiagnostics(BaseModel):
    """Optional, bounded Worker-local execution projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    reason: Literal[
        "OBSERVED",
        "READ_FAILED",
        "INVALID_PROJECTION",
        "UNCONFIGURED_BROKER",
        "UNSCOPED_CACHE",
        "BUDGET_EXCEEDED",
        "UNSUPPORTED_SOURCE",
        "UNAUTHENTICATED_SOURCE",
    ] = "READ_FAILED"
    broker_id: UUID | None = None
    captured_at: datetime | None = None
    rows: tuple[WorkerAutomationExecutionDiagnostics, ...] = Field(default=(), max_length=100)
    truncated: bool = False

    @field_validator("captured_at")
    @classmethod
    def normalize_captured_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        if self.observation == "UNKNOWN":
            if self.reason == "OBSERVED" or self.captured_at is not None or self.rows or self.truncated:
                raise ValueError("Unknown execution projection cannot contain financial rows")
        elif self.reason != "OBSERVED" or self.broker_id is None or self.captured_at is None:
            raise ValueError("Observed execution projection requires scope and capture time")
        return self


class WorkerDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_iterations: Count
    last_completed_at: datetime | None
    last_finished_at: datetime
    last_result: IterationResult
    error_code: Literal["ITERATION_FAILED"] | None = None
    outbox: OutboxDiagnostics
    broker_runtime: BrokerRuntimeDiagnostics = Field(default_factory=BrokerRuntimeDiagnostics)
    command_sync: CommandSyncDiagnostics | None = None
    execution_diagnostics: WorkerExecutionDiagnostics | None = None

    @field_validator("last_completed_at", "last_finished_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else floor_utc_millisecond(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.completed_iterations == 0) != (self.last_completed_at is None):
            raise ValueError("Completion count and timestamp disagree")
        if self.last_completed_at is not None and self.last_completed_at > self.last_finished_at:
            raise ValueError("Completion cannot follow the latest finished iteration")
        if self.last_result == "COMPLETED" and self.last_completed_at != self.last_finished_at:
            raise ValueError("Completed iteration must advance completion time")
        if (self.last_result == "ERROR") != (self.error_code == "ITERATION_FAILED"):
            raise ValueError("Error code and iteration result disagree")
        return self


class OutboxObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    status: Literal["OK", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    reason: Literal[
        "NOT_OBSERVED",
        "STALE",
        "READ_FAILED",
        "CLEAR",
        "PENDING",
        "FAILED",
        "OLD_PENDING",
    ]
    pending_count: Count | None = None
    failed_count: Count | None = None
    oldest_pending_at: str | None = None
    oldest_pending_age_ms: Count | None = None
    max_retry_count: Count | None = None


class WorkerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    status: Literal["OK", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    reason: Literal[
        "NOT_OBSERVED",
        "STALE",
        "CONTROL_PROGRESS",
        "CONTROL_STALLED",
        "OUTBOX_BLOCKED",
        "ITERATION_FAILED",
    ]
    instance_id: UUID | None = None
    received_at: str | None = None
    age_ms: Count | None = None
    completed_iterations: Count | None = None
    last_completed_at: str | None = None
    completion_age_ms: Count | None = None
    last_finished_at: str | None = None
    last_result: IterationResult | None = None
    error_code: Literal["ITERATION_FAILED"] | None = None
    outbox: OutboxObservation = Field(default_factory=lambda: OutboxObservation(reason="NOT_OBSERVED"))
    broker_runtime: BrokerRuntimeDiagnostics = Field(default_factory=BrokerRuntimeDiagnostics)
    command_sync: CommandSyncDiagnostics | None = None
