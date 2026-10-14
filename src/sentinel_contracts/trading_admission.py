"""Typed contracts for account-scoped runtime ownership and trade admission."""

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StringConstraints,
    model_validator,
)

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.time import require_utc_millisecond


def _reject_ambiguous_decimal(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("Money values must not use bool or float.")  # noqa: TRY004
    return value


def _require_finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Money values must be finite.")
    if value:
        _, digits, exponent = value.copy_abs().as_tuple()
        integer_digits = max(value.copy_abs().adjusted() + 1, 0)
        trailing_zeroes = 0
        while trailing_zeroes < len(digits) and digits[-trailing_zeroes - 1] == 0:
            trailing_zeroes += 1
        fractional_digits = max(0, -exponent - trailing_zeroes)
        if integer_digits > 19 or fractional_digits > 9:
            raise ValueError("Money values must fit NUMERIC(28, 9).")
    return value


ConfigurationFingerprint = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
_FiniteDecimal = Annotated[
    Decimal,
    BeforeValidator(_reject_ambiguous_decimal),
    AfterValidator(_require_finite),
    Field(max_digits=28, decimal_places=9),
]
PositiveFiniteDecimal = Annotated[_FiniteDecimal, Field(gt=0)]
NonNegativeFiniteDecimal = Annotated[_FiniteDecimal, Field(ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]
Revision = Annotated[int, Field(strict=True, ge=1)]
MillisecondUtc = Annotated[
    datetime,
    AfterValidator(require_utc_millisecond),
    PlainSerializer(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        return_type=str,
        when_used="json",
    ),
]


class StrictFrozenModel(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class AdmissionStatus(StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    STOP_REQUIRED = "STOP_REQUIRED"


class RuntimeRegistrationStatus(StrEnum):
    OWNER = "OWNER"
    PENDING = "PENDING"


class ReconciliationStatus(StrEnum):
    ACCOUNT_ONLY = "ACCOUNT_ONLY"
    CANARY_READY = "CANARY_READY"
    DATA_UNKNOWN = "DATA_UNKNOWN"


class SubmitReservationState(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    BROKER_ACTIVE = "BROKER_ACTIVE"
    UNKNOWN = "UNKNOWN"
    SETTLED = "SETTLED"
    CONFIRMED_NOT_SENT = "CONFIRMED_NOT_SENT"


class SubmitAbsenceBasis(StrEnum):
    RETIRED_GENERATION = "RETIRED_GENERATION"


class SubmitDenialReason(StrEnum):
    NOT_OWNER = "NOT_OWNER"
    DISARMED = "DISARMED"
    STOP_REQUIRED = "STOP_REQUIRED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    UID_NOT_ALLOWED = "UID_NOT_ALLOWED"
    STALE_MARKET = "STALE_MARKET"
    ORDER_NOTIONAL_EXCEEDED = "ORDER_NOTIONAL_EXCEEDED"
    EXPOSURE_EXCEEDED = "EXPOSURE_EXCEEDED"
    OUTSTANDING_ORDERS_EXCEEDED = "OUTSTANDING_ORDERS_EXCEEDED"
    DAILY_LOSS_EXCEEDED = "DAILY_LOSS_EXCEEDED"
    DATA_UNKNOWN = "DATA_UNKNOWN"
    FOREIGN_ACTIVE_ORDER = "FOREIGN_ACTIVE_ORDER"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    BASELINE_REQUIRED = "BASELINE_REQUIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ALREADY_CONSUMED = "ALREADY_CONSUMED"
    CORE_UNAVAILABLE = "CORE_UNAVAILABLE"


class SubmitAttemptView(StrictFrozenModel):
    broker_id: UUID
    account_id: str = Field(min_length=1)
    idempotency_key: UUID
    consumed_state: SubmitReservationState | None
    proven_not_granted: StrictBool
    original_runtime_instance_id: UUID | None
    original_generation: StrictPositiveInt | None
    original_generation_retired: StrictBool
    absence_basis: SubmitAbsenceBasis | None

    @model_validator(mode="after")
    def validate_attempt_state(self):
        has_origin = self.original_runtime_instance_id is not None and self.original_generation is not None
        if (self.original_runtime_instance_id is None) != (self.original_generation is None):
            raise ValueError("Submit attempt origin requires both runtime instance and generation.")
        if self.consumed_state is not None and (
            not has_origin or self.proven_not_granted or self.absence_basis is not None
        ):
            raise ValueError("Consumed attempts require their original owner and cannot prove absence.")
        if self.proven_not_granted and (
            self.consumed_state is not None
            or not has_origin
            or not self.original_generation_retired
            or self.absence_basis is not SubmitAbsenceBasis.RETIRED_GENERATION
        ):
            raise ValueError("No-grant evidence is conclusive only for a retired original generation.")
        if not self.proven_not_granted and self.absence_basis is not None:
            raise ValueError("An absence basis requires a proven no-grant result.")
        return self


class SubmitAttemptRequest(StrictFrozenModel):
    original_runtime_instance_id: UUID | None = None
    original_generation: StrictPositiveInt | None = None

    @model_validator(mode="after")
    def validate_origin_pair(self):
        if (self.original_runtime_instance_id is None) != (self.original_generation is None):
            raise ValueError("Submit attempt origin requires both runtime instance and generation.")
        return self


class RuntimeRegistrationRequest(StrictFrozenModel):
    worker_id: str = Field(min_length=1, max_length=128)
    runtime_instance_id: UUID
    broker_id: UUID
    application_environment: Literal["TEST", "PROD"]
    access_mode: Literal["READ_ONLY", "TRADE"]
    strategy_enabled: StrictBool


class RuntimeRegistrationResult(StrictFrozenModel):
    runtime_instance_id: UUID
    generation: StrictPositiveInt | None
    registration_status: RuntimeRegistrationStatus
    admission_status: AdmissionStatus
    lease_expires_at: MillisecondUtc | None

    @model_validator(mode="after")
    def validate_owner_lease(self):
        if self.registration_status is RuntimeRegistrationStatus.PENDING:
            if self.generation is not None or self.lease_expires_at is not None:
                raise ValueError("Pending runtime registration has no generation or owner lease.")
        elif self.generation is None or self.lease_expires_at is None:
            raise ValueError("Owner runtime registration requires a generation and lease expiry.")
        return self


class AdmissionLimits(StrictFrozenModel):
    canary_uid: str = Field(min_length=1, max_length=128)
    currency: Literal["RUB"]
    max_order_notional: PositiveFiniteDecimal
    exposure_scope: Literal["CANARY_UID_GROSS"]
    max_exposure: PositiveFiniteDecimal
    max_outstanding_orders: StrictPositiveInt
    max_daily_loss: PositiveFiniteDecimal


class AdmissionBaseline(StrictFrozenModel):
    baseline_id: UUID
    broker_id: UUID
    account_id: str = Field(min_length=1)
    moscow_day: date
    timezone: Literal["Europe/Moscow"]
    currency: Literal["RUB"]
    equity: NonNegativeFiniteDecimal
    net_external_cash_flow: _FiniteDecimal
    capture_started_at: MillisecondUtc
    capture_finished_at: MillisecondUtc
    source: Literal["GET_PORTFOLIO"]
    operations_through: MillisecondUtc
    accepted_by: str = Field(min_length=1, max_length=128)
    accepted_at: MillisecondUtc
    policy: Literal["FIRST_ACCEPTED_ACCOUNT_EQUITY"]

    @model_validator(mode="after")
    def validate_capture_window(self):
        if (
            self.capture_started_at > self.capture_finished_at
            or self.operations_through > self.capture_finished_at
            or self.accepted_at < self.capture_finished_at
        ):
            raise ValueError("Baseline operations cannot extend beyond the captured portfolio snapshot.")
        return self


class CaptureBaselineRequest(StrictFrozenModel):
    day: date
    accept_policy: Literal[True]


class AdmissionView(StrictFrozenModel):
    broker_id: UUID
    account_id: str = Field(min_length=1)
    revision: Revision
    owner_runtime_instance_id: UUID | None
    owner_generation: StrictPositiveInt | None
    recovery_runtime_instance_id: UUID | None
    admission_status: AdmissionStatus
    arm_revision: Revision | None
    lease_expires_at: MillisecondUtc | None
    arm_expires_at: MillisecondUtc | None
    limits: AdmissionLimits | None
    configuration_fingerprint: ConfigurationFingerprint | None
    baseline: AdmissionBaseline | None
    reconciliation: "ReconciliationResult | None"
    effective_can_submit: StrictBool
    effective_denial_reasons: tuple[SubmitDenialReason, ...]
    unresolved_reservation_count: Annotated[int, Field(strict=True, ge=0)]
    outstanding_order_count: Annotated[int, Field(strict=True, ge=0)]
    recovery_only: StrictBool


class ReconciliationRequest(StrictFrozenModel):
    expected_revision: Revision
    canary_uid: str | None = Field(default=None, min_length=1, max_length=128)


class ReconciliationResult(StrictFrozenModel):
    reconciliation_id: UUID
    broker_id: UUID
    account_id: str = Field(min_length=1)
    scope: Literal["ACCOUNT", "CANARY_UID"]
    owner_runtime_instance_id: UUID
    owner_generation: StrictPositiveInt
    configuration_fingerprint: ConfigurationFingerprint
    status: ReconciliationStatus
    canary_uid: str | None = Field(default=None, min_length=1, max_length=128)
    captured_at: MillisecondUtc
    valid_until: MillisecondUtc
    can_arm: StrictBool
    position_anchor_units: NonNegativeFiniteDecimal | None
    active_ordinary_order_count: Annotated[int, Field(strict=True, ge=0)]
    active_stop_order_count: Annotated[int, Field(strict=True, ge=0)]
    unresolved_reservation_count: Annotated[int, Field(strict=True, ge=0)]
    reasons: tuple[SubmitDenialReason, ...]
    risk_snapshot_id: UUID | None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope == "ACCOUNT" and self.canary_uid is not None:
            raise ValueError("Account reconciliation cannot identify a canary UID.")
        if self.scope == "CANARY_UID" and self.canary_uid is None:
            raise ValueError("Canary reconciliation requires a UID.")
        if self.valid_until < self.captured_at:
            raise ValueError("Reconciliation cannot expire before capture.")
        return self


class ArmRequest(StrictFrozenModel):
    expected_revision: Revision
    runtime_instance_id: UUID
    baseline_id: UUID
    reconciliation_id: UUID
    limits: AdmissionLimits
    configuration_fingerprint: ConfigurationFingerprint
    expires_at: MillisecondUtc


class RevokeRequest(StrictFrozenModel):
    expected_revision: Revision
    reason: str = Field(min_length=1, max_length=240)


class OwnerStoppedRequest(StrictFrozenModel):
    expected_revision: Revision
    old_runtime_instance_id: UUID
    old_generation: StrictPositiveInt
    candidate_runtime_instance_id: UUID
    stopped_at: MillisecondUtc
    auto_restart_disabled: Literal[True]
    evidence_ref: str = Field(min_length=1, max_length=500)


class SubmitAuthorizationRequest(StrictFrozenModel):
    broker_id: UUID
    account_id: str = Field(min_length=1)
    automation_id: UUID
    idempotency_key: UUID
    generation: StrictPositiveInt
    revision: Revision
    configuration_fingerprint: ConfigurationFingerprint
    instrument_uid: str = Field(min_length=1, max_length=128)
    lot_size: StrictPositiveInt
    currency: Literal["RUB"]
    side: Literal["BUY", "SELL"]
    quantity_lots: StrictPositiveInt
    limit_price: PositiveFiniteDecimal
    market_valid_until: MillisecondUtc
    submit_deadline: MillisecondUtc

    @model_validator(mode="after")
    def validate_submit_deadline(self):
        if self.submit_deadline > self.market_valid_until:
            raise ValueError("Submit deadline cannot outlive the market snapshot.")
        return self


def submit_request_fingerprint(request: SubmitAuthorizationRequest) -> str:
    """Hash the immutable economic identity for an idempotent broker submit."""
    price = format(request.limit_price, "f")
    if "." in price:
        price = price.rstrip("0").rstrip(".")
    canonical = {
        "account_id": request.account_id,
        "automation_id": str(request.automation_id),
        "broker_id": str(request.broker_id),
        "currency": request.currency,
        "idempotency_key": str(request.idempotency_key),
        "instrument_uid": request.instrument_uid,
        "limit_price": price,
        "lot_size": request.lot_size,
        "quantity_lots": request.quantity_lots,
        "side": request.side,
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return sha256(payload).hexdigest()


class SubmitPermit(StrictFrozenModel):
    result: Literal["ALLOWED"] = "ALLOWED"
    permit_id: UUID
    runtime_instance_id: UUID
    idempotency_key: UUID
    generation: StrictPositiveInt
    revision: Revision
    configuration_fingerprint: ConfigurationFingerprint
    request_fingerprint: ConfigurationFingerprint
    issued_at: MillisecondUtc
    expires_at: MillisecondUtc
    server_response_at: MillisecondUtc
    risk_snapshot_id: UUID

    @model_validator(mode="after")
    def validate_response_deadline(self):
        if self.issued_at > self.server_response_at:
            raise ValueError("Permit issue time cannot follow its server response time.")
        if self.server_response_at >= self.expires_at:
            raise ValueError("A permit response at or after its deadline is not usable.")
        return self


class SubmitDenied(StrictFrozenModel):
    result: Literal["DENIED"] = "DENIED"
    reason: SubmitDenialReason
    outcome_uncertain: StrictBool
    attempt: SubmitAttemptView | None = None

    @model_validator(mode="after")
    def validate_uncertain_consumed_key(self):
        if self.reason is SubmitDenialReason.ALREADY_CONSUMED and not self.outcome_uncertain:
            raise ValueError("An already-consumed idempotency key must remain outcome-uncertain.")
        if self.attempt is not None and self.attempt.consumed_state is not None and not self.outcome_uncertain:
            raise ValueError("A consumed submit attempt cannot be reported as a definite no-send.")
        return self


class PreRpcAbortProof(StrictFrozenModel):
    permit_id: UUID
    idempotency_key: UUID
    request_fingerprint: ConfigurationFingerprint
    runtime_instance_id: UUID
    generation: StrictPositiveInt
    configuration_fingerprint: ConfigurationFingerprint
    aborted_at: MillisecondUtc
    reason: Literal["PERMIT_EXPIRED_BEFORE_POST", "MARKET_EXPIRED_BEFORE_POST"]


SubmitAuthorizationResult: TypeAlias = Annotated[
    SubmitPermit | SubmitDenied,
    Field(discriminator="result"),
]
