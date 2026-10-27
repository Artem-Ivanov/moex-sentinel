"""Atomic transaction owner for all Core trading fact repositories."""

from types import TracebackType

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import FactRuntimeAuthorization, FactSubmitReservation
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.automation_facts import AutomationFactsRepository
from moex_sentinel.storage.repositories.order_facts import OrderFactsRepository
from moex_sentinel.storage.repositories.position_ledger import PositionLedgerRepository
from moex_sentinel.storage.repositories.trading_analytics import TradingAnalyticsRepository
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository


class TradingFactsUnitOfWork:
    """Commit one automation fact group or roll its aggregate changes back."""

    automations: AutomationFactsRepository
    orders: OrderFactsRepository
    positions: PositionLedgerRepository
    audit: TradingAuditRepository
    analytics: TradingAnalyticsRepository

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> "TradingFactsUnitOfWork":
        session = self._factory()
        self._session = session
        self.automations = AutomationFactsRepository(session)
        self.orders = OrderFactsRepository(session)
        self.positions = PositionLedgerRepository(session)
        self.audit = TradingAuditRepository(session)
        self.analytics = TradingAnalyticsRepository(session)
        return self

    def scope_matches_environment(self, user_broker_id: str, environment: str) -> bool:
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        return (
            self._session.scalar(
                select(UserBrokerModel.id)
                .where(UserBrokerModel.id == user_broker_id, UserBrokerModel.environment == environment)
                .with_for_update()
            )
            is not None
        )

    def authorize_fact_runtime(
        self,
        user_broker_id: str,
        runtime_instance_id: str,
        generation: int,
        *,
        environment: str,
    ) -> FactRuntimeAuthorization | None:
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        admission = self._session.scalar(
            select(TradingAccountAdmissionModel)
            .where(TradingAccountAdmissionModel.user_broker_id == user_broker_id)
            .with_for_update()
        )
        if admission is None:
            return None
        runtime = self._session.scalar(
            select(WorkerRuntimeInstanceModel)
            .where(
                WorkerRuntimeInstanceModel.user_broker_id == user_broker_id,
                WorkerRuntimeInstanceModel.runtime_instance_id == runtime_instance_id,
            )
            .with_for_update()
        )
        if (
            runtime is None
            or runtime.environment != environment
            or runtime.registration_status != "OWNER"
            or runtime.generation != generation
            or admission.owner_runtime_instance_id != runtime_instance_id
            or admission.owner_generation != generation
            or (admission.recovery_only and admission.recovery_runtime_instance_id != runtime_instance_id)
        ):
            return None
        return FactRuntimeAuthorization(recovery_only=admission.recovery_only)

    def get_fact_submit_reservation(self, user_broker_id: str, idempotency_key: str) -> FactSubmitReservation | None:
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        reservation = self._session.scalar(
            select(TradingSubmitReservationModel)
            .where(
                TradingSubmitReservationModel.user_broker_id == user_broker_id,
                TradingSubmitReservationModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if reservation is None:
            return None
        origin = self._session.get(WorkerRuntimeInstanceModel, reservation.runtime_instance_id)
        return FactSubmitReservation(
            automation_id=reservation.automation_id,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            state=reservation.state,
            runtime_instance_id=reservation.runtime_instance_id,
            generation=reservation.generation,
            configuration_fingerprint=reservation.configuration_fingerprint,
            permit_id=reservation.permit_id,
            side=reservation.side,
            quantity_lots=reservation.quantity_lots,
            limit_price=reservation.limit_price,
            requested_amount=reservation.requested_amount,
            reserved_cash=reservation.reserved_cash,
            reserved_position_units=reservation.reserved_position_units,
            payload=dict(reservation.payload),
            origin_registration_status=None if origin is None else origin.registration_status,
        )

    def confirm_submit_not_sent(self, user_broker_id: str, idempotency_key: str, *, now) -> bool:
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        reservation = self._session.scalar(
            select(TradingSubmitReservationModel)
            .where(
                TradingSubmitReservationModel.user_broker_id == user_broker_id,
                TradingSubmitReservationModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if reservation is None or reservation.state not in {"IN_FLIGHT", "UNKNOWN"}:
            return False
        reservation.state = "CONFIRMED_NOT_SENT"
        reservation.reserved_cash = 0
        reservation.reserved_position_units = 0
        reservation.updated_at = now
        return True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is None:
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            else:
                session.rollback()
        finally:
            session.close()
            self._session = None
