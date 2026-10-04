"""Safe volatile control progress and Worker-owned outbox observations."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sentinel_contracts.time import floor_utc_millisecond

Count = Annotated[int, Field(ge=0, strict=True)]
IterationResult = Literal["COMPLETED", "OUTBOX_BLOCKED", "ERROR"]


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
                for value in (self.pending_count, self.failed_count, self.oldest_pending_at, self.max_retry_count)
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


class WorkerDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_iterations: Count
    last_completed_at: datetime | None
    last_finished_at: datetime
    last_result: IterationResult
    error_code: Literal["ITERATION_FAILED"] | None = None
    outbox: OutboxDiagnostics

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
    reason: Literal["NOT_OBSERVED", "STALE", "READ_FAILED", "CLEAR", "PENDING", "FAILED", "OLD_PENDING"]
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
        "NOT_OBSERVED", "STALE", "CONTROL_PROGRESS", "CONTROL_STALLED", "OUTBOX_BLOCKED", "ITERATION_FAILED"
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
