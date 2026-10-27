"""Normalized evidence for fees reported after broker execution."""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import Field, field_validator

from moex_sentinel.domain.portfolio import ExternalOperation
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.time import floor_utc_millisecond

MONEY_QUANTUM = Decimal("0.000000001")
MONEY_MAX = Decimal("10000000000000000000")


class BrokerFeeObservation(PositionalModel):
    """One exact parent-linked, absolute broker fee observation."""

    user_broker_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    parent_operation_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    instrument_id: str | None = None
    currency: str = Field(min_length=1, max_length=8)
    amount: Decimal = Field(gt=0)
    occurred_at: datetime

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("Currency is required.")
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return floor_utc_millisecond(value)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value >= MONEY_MAX:
            raise ValueError("Fee amount is outside the supported money range.")
        try:
            if value.quantize(MONEY_QUANTUM) != value:
                raise ValueError("Fee amount has more than 9 decimal places.")
        except InvalidOperation as error:
            raise ValueError("Fee amount is outside the supported money range.") from error
        return value


class BrokerFeeScanClaim(PositionalModel):
    """One fair, compare-and-swap claim for an account history page."""

    user_broker_id: str
    account_id: str
    scan_pass_id: str
    claim_id: str
    lower_bound: datetime
    upper_bound: datetime
    cursor: str | None


def normalize_broker_fee(user_broker_id: str, operation: ExternalOperation) -> BrokerFeeObservation | None:
    """Convert only executed BROKER_FEE payments; nested operation commissions are not separate fees."""
    operation_type = operation.operation_type.removeprefix("OPERATION_TYPE_")
    operation_state = operation.state.removeprefix("OPERATION_STATE_")
    if operation_type != "BROKER_FEE":
        return None
    payment = operation.payment
    parent_id = operation.parent_operation_id
    if operation_state != "EXECUTED" or payment is None or not parent_id or not parent_id.strip():
        raise ValueError("Broker fee evidence is incomplete.")
    if payment.amount >= 0 or not payment.amount.is_finite():
        raise ValueError("Broker fee payment must be a finite negative amount.")
    return BrokerFeeObservation(
        user_broker_id=user_broker_id,
        account_id=operation.account_id,
        parent_operation_id=parent_id.strip(),
        operation_id=operation.operation_id,
        instrument_id=operation.instrument_id,
        currency=payment.currency,
        amount=-payment.amount,
        occurred_at=operation.occurred_at,
    )
