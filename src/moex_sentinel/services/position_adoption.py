"""Validation and orchestration for clean-start broker positions."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from moex_sentinel.domain.instrument_catalog import UserBrokerCatalogInstrument
from moex_sentinel.domain.position_adoption import (
    PositionAdoptionCandidate,
    PositionAdoptionConflictError,
    PositionAdoptionDiagnostic,
    PositionAdoptionReason,
    PositionAdoptionResult,
    PositionAdoptionWriteResult,
)
from moex_sentinel.services.portfolio_ports import PositionAdoptionBrokerPort


class PositionAdoptionRepositoryPort(Protocol):
    def find_instrument(
        self,
        user_broker_id: str,
        external_instrument_id: str,
    ) -> UserBrokerCatalogInstrument | None: ...

    def adopt(self, candidate: PositionAdoptionCandidate) -> PositionAdoptionWriteResult: ...


class PositionAdoptionService:
    def __init__(
        self,
        repository: PositionAdoptionRepositoryPort,
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def adopt(
        self,
        user_broker_id: str,
        account_id: str,
        broker: PositionAdoptionBrokerPort,
    ) -> PositionAdoptionResult:
        adopted = existing = held = skipped = 0
        diagnostics: list[PositionAdoptionDiagnostic] = []
        for position in await broker.get_positions(account_id):
            reason = None
            if position.quantity_lots == 0:
                skipped += 1
                continue
            if position.quantity_lots < 0 or position.quantity_lots != position.quantity_lots.to_integral_value():
                reason = PositionAdoptionReason.BOOTSTRAP_INVALID_QUANTITY
            instrument = self._repository.find_instrument(user_broker_id, position.instrument_id)
            if reason is None and instrument is None:
                reason = PositionAdoptionReason.BOOTSTRAP_INSTRUMENT_NOT_FOUND
            if reason is None and (position.average_price is None or position.average_price.amount <= 0):
                reason = PositionAdoptionReason.BOOTSTRAP_PRICE_UNAVAILABLE
            if (
                reason is None
                and instrument is not None
                and position.average_price is not None
                and position.average_price.currency.upper() != instrument.currency.upper()
            ):
                reason = PositionAdoptionReason.BOOTSTRAP_CURRENCY_MISMATCH
            if reason is None and await broker.list_active_orders(account_id, position.instrument_id):
                reason = PositionAdoptionReason.BOOTSTRAP_ACTIVE_ORDER
            if reason is not None:
                held += 1
                diagnostics.append(PositionAdoptionDiagnostic(account_id, position.instrument_id, reason))
                continue
            if instrument is None or position.average_price is None:
                raise RuntimeError("Validated position adoption context is incomplete.")
            quantity_lots = int(position.quantity_lots)
            candidate = PositionAdoptionCandidate.create(
                user_broker_id=user_broker_id,
                instrument_id=instrument.id,
                quantity_lots=quantity_lots,
                average_price=position.average_price.amount,
                invested_amount=position.average_price.amount * Decimal(instrument.lot_size) * quantity_lots,
                currency=instrument.currency.upper(),
                observed_at=self._clock(),
            )
            try:
                outcome = self._repository.adopt(candidate)
            except PositionAdoptionConflictError:
                held += 1
                diagnostics.append(
                    PositionAdoptionDiagnostic(
                        account_id,
                        position.instrument_id,
                        PositionAdoptionReason.BOOTSTRAP_CONFLICT,
                    )
                )
            else:
                if outcome is PositionAdoptionWriteResult.ADOPTED:
                    adopted += 1
                else:
                    existing += 1
        return PositionAdoptionResult(adopted, existing, held, skipped, tuple(diagnostics))
