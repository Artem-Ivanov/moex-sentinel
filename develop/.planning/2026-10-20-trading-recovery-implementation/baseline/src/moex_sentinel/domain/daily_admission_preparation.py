"""Safe, volatile observation of automatic daily admission preparation."""

from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    ConfigurationFingerprint,
    MillisecondUtc,
    StrictPositiveInt,
    SubmitDenialReason,
)


class DailyAdmissionPreparationStatus(StrEnum):
    DISABLED = "DISABLED"
    WAITING = "WAITING"
    BASELINE_ACCEPTED = "BASELINE_ACCEPTED"
    PREPARED = "PREPARED"
    BLOCKED = "BLOCKED"


class DailyAdmissionPreparationReason(StrEnum):
    DAILY_PREPARATION_DISABLED = "DAILY_PREPARATION_DISABLED"
    NOT_OBSERVED = "NOT_OBSERVED"
    SCOPE_UNAVAILABLE = "SCOPE_UNAVAILABLE"
    OWNER_MISSING = "OWNER_MISSING"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    BASELINE_REQUIRED = "BASELINE_REQUIRED"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
    SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
    MOSCOW_DAY_CHANGED = "MOSCOW_DAY_CHANGED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RECONCILIATION_EXPIRED = "RECONCILIATION_EXPIRED"
    ARM_REQUIRED = "ARM_REQUIRED"
    RISK_DATA_UNKNOWN = "RISK_DATA_UNKNOWN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CORE_UNAVAILABLE = "CORE_UNAVAILABLE"


class DailyAdmissionPreparationView(PositionalModel):
    """Public preparation status without broker account or SDK details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    moscow_day: date | None
    captured_at: MillisecondUtc | None
    status: DailyAdmissionPreparationStatus
    reason: DailyAdmissionPreparationReason | None
    baseline_id: UUID | None
    owner_runtime_instance_id: UUID | None
    owner_generation: StrictPositiveInt | None
    configuration_fingerprint: ConfigurationFingerprint | None
    reconciliation_id: UUID | None
    reconciliation_valid_until: MillisecondUtc | None
    suggested_limits: AdmissionLimits | None
    risk_reasons: tuple[SubmitDenialReason, ...]

    @model_validator(mode="after")
    def validate_observation(self):
        for field in ("captured_at", "reconciliation_valid_until"):
            value = getattr(self, field)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"{field} must be timezone-aware.")
                if floor_utc_millisecond(value) != value:
                    raise ValueError(f"{field} must have millisecond precision.")
        if (self.owner_runtime_instance_id is None) != (self.owner_generation is None):
            raise ValueError("Owner ID and generation must be present together.")
        if (self.reconciliation_id is None) != (self.reconciliation_valid_until is None):
            raise ValueError("Reconciliation ID and validity must be present together.")
        if self.status is DailyAdmissionPreparationStatus.DISABLED:
            if self.reason is not DailyAdmissionPreparationReason.DAILY_PREPARATION_DISABLED:
                raise ValueError("Disabled preparation requires its fixed safe reason.")
        elif self.status is DailyAdmissionPreparationStatus.WAITING:
            if self.reason is None:
                raise ValueError("Waiting preparation requires a fixed safe reason.")
        elif self.status is DailyAdmissionPreparationStatus.BASELINE_ACCEPTED:
            if self.baseline_id is None or self.owner_runtime_instance_id is None:
                raise ValueError("Accepted daily baseline requires its immutable baseline and owner identity.")
        elif self.status is DailyAdmissionPreparationStatus.PREPARED:
            if (
                self.reason is not DailyAdmissionPreparationReason.ARM_REQUIRED
                or self.baseline_id is None
                or self.owner_runtime_instance_id is None
                or self.reconciliation_id is None
                or self.suggested_limits is None
            ):
                raise ValueError("Prepared means fresh canary reconciliation and preview caps, pending explicit ARM.")
        elif self.status is DailyAdmissionPreparationStatus.BLOCKED and self.reason is None:
            raise ValueError("Blocked preparation requires a fixed safe reason.")
        return self
