"""Typed values for adopting current broker positions into a clean runtime."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field

from sentinel_contracts.base import PositionalModel


class PositionAdoptionReason(StrEnum):
    BOOTSTRAP_INVALID_QUANTITY = "BOOTSTRAP_INVALID_QUANTITY"
    BOOTSTRAP_PRICE_UNAVAILABLE = "BOOTSTRAP_PRICE_UNAVAILABLE"
    BOOTSTRAP_INSTRUMENT_NOT_FOUND = "BOOTSTRAP_INSTRUMENT_NOT_FOUND"
    BOOTSTRAP_ACTIVE_ORDER = "BOOTSTRAP_ACTIVE_ORDER"
    BOOTSTRAP_COMMISSION_UNAVAILABLE = "BOOTSTRAP_COMMISSION_UNAVAILABLE"
    BOOTSTRAP_CURRENCY_MISMATCH = "BOOTSTRAP_CURRENCY_MISMATCH"
    BOOTSTRAP_CONFLICT = "BOOTSTRAP_CONFLICT"


class PositionAdoptionWriteResult(StrEnum):
    ADOPTED = "ADOPTED"
    EXISTING = "EXISTING"


class PositionAdoptionConflictError(RuntimeError):
    """An active automation has a different immutable bootstrap snapshot."""


class PositionAdoptionCandidate(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    automation_id: UUID
    position_cycle_id: UUID
    position_lot_id: UUID
    user_broker_id: str
    instrument_id: str
    quantity_lots: int = Field(gt=0)
    average_price: Decimal = Field(gt=0)
    invested_amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=1)
    observed_at: datetime

    @classmethod
    def create(
        cls,
        *,
        user_broker_id: str,
        instrument_id: str,
        quantity_lots: int,
        average_price: Decimal,
        invested_amount: Decimal,
        currency: str,
        observed_at: datetime,
    ) -> "PositionAdoptionCandidate":
        identity = f"moex-sentinel:{user_broker_id}:{instrument_id}"
        return cls(
            automation_id=uuid5(NAMESPACE_URL, f"{identity}:automation"),
            position_cycle_id=uuid5(NAMESPACE_URL, f"{identity}:position-cycle"),
            position_lot_id=uuid5(NAMESPACE_URL, f"{identity}:position-lot"),
            user_broker_id=user_broker_id,
            instrument_id=instrument_id,
            quantity_lots=quantity_lots,
            average_price=average_price,
            invested_amount=invested_amount,
            currency=currency,
            observed_at=observed_at,
        )

    def after_closed_automation(self) -> "PositionAdoptionCandidate":
        """Derive the next position identity without depending on observation time."""
        identity = f"moex-sentinel:{self.user_broker_id}:{self.instrument_id}:after:{self.automation_id}"
        return self.model_copy(
            update={
                "automation_id": uuid5(NAMESPACE_URL, f"{identity}:automation"),
                "position_cycle_id": uuid5(NAMESPACE_URL, f"{identity}:position-cycle"),
                "position_lot_id": uuid5(NAMESPACE_URL, f"{identity}:position-lot"),
            }
        )


class PositionAdoptionDiagnostic(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    external_instrument_id: str
    reason: PositionAdoptionReason


class PositionAdoptionResult(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adopted: int = 0
    existing: int = 0
    held: int = 0
    skipped: int = 0
    diagnostics: tuple[PositionAdoptionDiagnostic, ...] = ()
