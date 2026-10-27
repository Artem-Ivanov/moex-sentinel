"""Atomic transaction owner for all Core trading fact repositories."""

from datetime import datetime
from decimal import Decimal
from types import TracebackType

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import FactRuntimeAuthorization, FactSubmitReservation
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeExecutionModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
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
from sentinel_contracts.time import require_utc_millisecond


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

    def execution_diagnostics_scope(
        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
    ) -> dict[str, object] | None:
        """Read owner, instrument and reservations for a scoped diagnostics response without locks."""
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        automation = self._session.execute(
            select(
                TradingAutomationModel.id,
                TradingAutomationModel.instrument_id,
                UserBrokerModel.external_account_id.label("account_id"),
                BrokerInstrumentModel.external_instrument_id,
                BrokerInstrumentModel.lot_size,
                BrokerInstrumentModel.currency,
            )
            .join(
                BrokerInstrumentModel,
                (BrokerInstrumentModel.id == TradingAutomationModel.instrument_id)
                & (BrokerInstrumentModel.user_broker_id == TradingAutomationModel.user_broker_id),
            )
            .join(UserBrokerModel, UserBrokerModel.id == TradingAutomationModel.user_broker_id)
            .where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
            )
        ).one_or_none()
        if automation is None:
            return None
        owner = self._session.execute(
            select(
                TradingAccountAdmissionModel.owner_runtime_instance_id,
                TradingAccountAdmissionModel.owner_generation,
            ).where(TradingAccountAdmissionModel.user_broker_id == user_broker_id)
        ).one_or_none()
        reservation_query = select(
            TradingSubmitReservationModel.idempotency_key,
            TradingSubmitReservationModel.state,
            TradingSubmitReservationModel.runtime_instance_id,
            TradingSubmitReservationModel.generation,
            TradingSubmitReservationModel.side,
            TradingSubmitReservationModel.quantity_lots,
            TradingSubmitReservationModel.reserved_cash,
            TradingSubmitReservationModel.reserved_position_units,
            TradingSubmitReservationModel.payload["settlement_reason"].as_string().label("settlement_reason"),
            TradingSubmitReservationModel.payload["broker_outcome_unknown"].as_boolean().label(
                "broker_outcome_unknown"
            ),
            TradingSubmitReservationModel.created_at,
            TradingSubmitReservationModel.updated_at,
        ).where(
            TradingSubmitReservationModel.user_broker_id == user_broker_id,
            TradingSubmitReservationModel.automation_id == automation_id,
        )
        if intent_id is not None:
            reservation_query = reservation_query.where(TradingSubmitReservationModel.idempotency_key == intent_id)
        reservations = self._session.execute(
            reservation_query.order_by(TradingSubmitReservationModel.updated_at.desc()).limit(limit + 1)
        ).all()
        return {
            "automation_id": automation.id,
            "instrument_id": automation.instrument_id,
            "account_id": automation.account_id,
            "external_instrument_id": automation.external_instrument_id,
            "lot_size": automation.lot_size,
            "currency": automation.currency,
            "owner_runtime_instance_id": None if owner is None else owner.owner_runtime_instance_id,
            "owner_generation": None if owner is None else owner.owner_generation,
            "reservation_truncated": len(reservations) > limit,
            "reservations": tuple(
                {
                    "intent_id": row.idempotency_key,
                    "state": row.state,
                    "side": row.side,
                    "origin_runtime_instance_id": row.runtime_instance_id,
                    "origin_generation": row.generation,
                    "quantity_lots": row.quantity_lots,
                    "reserved_cash": row.reserved_cash,
                    "reserved_position_units": row.reserved_position_units,
                    "settlement_reason": row.settlement_reason if row.settlement_reason == "AGE_POLICY" else None,
                    "broker_outcome_unknown": row.broker_outcome_unknown
                    if type(row.broker_outcome_unknown) is bool
                    else None,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in reservations[:limit]
            ),
        }

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

    def record_age_policy_candidate(self, user_broker_id: str, idempotency_key: str, *, expired_at: datetime) -> bool:
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        expired_at = require_utc_millisecond(expired_at)
        reservation = self._session.scalar(
            select(TradingSubmitReservationModel)
            .where(
                TradingSubmitReservationModel.user_broker_id == user_broker_id,
                TradingSubmitReservationModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if reservation is None or reservation.state not in {"IN_FLIGHT", "BROKER_ACTIVE", "UNKNOWN"}:
            return False
        candidate = {
            "version": 1,
            "expired_at": expired_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        existing = reservation.payload.get("_age_policy")
        if existing is not None and existing != candidate:
            return False
        reservation.payload = {**reservation.payload, "_age_policy": candidate}
        return True

    def sync_age_policy_settled_execution(self, user_broker_id: str, order_id: str) -> None:
        """Record late Core executions only when they fully match the durable terminal order."""
        if self._session is None:
            raise RuntimeError("Fact unit of work is not active.")
        order = self._session.scalar(
            select(BrokerOrderModel).where(
                BrokerOrderModel.user_broker_id == user_broker_id,
                BrokerOrderModel.id == order_id,
            )
        )
        if order is None or order.terminal_at is None:
            return
        reservation = self._session.scalar(
            select(TradingSubmitReservationModel)
            .where(
                TradingSubmitReservationModel.user_broker_id == user_broker_id,
                TradingSubmitReservationModel.idempotency_key == order.idempotency_key,
                TradingSubmitReservationModel.state == "SETTLED",
            )
            .with_for_update()
        )
        if (
            reservation is None
            or reservation.payload.get("settlement_reason") != "AGE_POLICY"
            or not isinstance(reservation.payload.get("_age_policy"), dict)
        ):
            return
        executions = self._session.scalars(
            select(TradeExecutionModel).where(
                TradeExecutionModel.user_broker_id == user_broker_id,
                TradeExecutionModel.broker_order_id == order_id,
            )
        ).all()
        executed_lots = sum(item.executed_lots for item in executions)
        executed_amount = sum((item.value for item in executions), Decimal(0))
        executed_commission = sum((item.broker_commission + item.other_fees for item in executions), Decimal(0))
        if (
            executed_lots > reservation.quantity_lots
            or executed_amount != order.executed_amount
            or executed_commission != order.executed_commission
            or (executed_lots == 0 and executed_amount != 0)
            or (executed_lots > 0 and executed_amount <= 0)
        ):
            return
        reservation.payload = {
            **reservation.payload,
            "broker_outcome_unknown": False,
            "last_reconciled_order_state": order.state,
            "executed_lots": executed_lots,
            "executed_amount": str(executed_amount),
            "executed_commission": str(executed_commission),
        }

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
