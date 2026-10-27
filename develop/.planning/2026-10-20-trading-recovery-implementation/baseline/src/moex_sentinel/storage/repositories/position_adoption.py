"""Atomic persistence for immutable broker-position bootstrap snapshots."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.instrument_catalog import UserBrokerCatalogInstrument
from moex_sentinel.domain.position_adoption import (
    PositionAdoptionCandidate,
    PositionAdoptionConflictError,
    PositionAdoptionWriteResult,
)
from moex_sentinel.storage.database import session_scope
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.repositories.reference_catalog import _record as instrument_record


class PositionAdoptionRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def find_instrument(
        self,
        user_broker_id: str,
        external_instrument_id: str,
    ) -> UserBrokerCatalogInstrument | None:
        with self._factory() as session:
            model = session.scalar(
                select(BrokerInstrumentModel).where(
                    BrokerInstrumentModel.user_broker_id == user_broker_id,
                    BrokerInstrumentModel.external_instrument_id == external_instrument_id,
                    BrokerInstrumentModel.is_active.is_(True),
                )
            )
            return None if model is None else instrument_record(model)

    def adopt(self, candidate: PositionAdoptionCandidate) -> PositionAdoptionWriteResult:
        try:
            with session_scope(self._factory) as session:
                existing = self._active(session, candidate)
                if existing is not None:
                    return self._compare(existing, candidate)
                closed_ids = set(
                    session.scalars(
                        select(TradingAutomationModel.id).where(
                            TradingAutomationModel.user_broker_id == candidate.user_broker_id,
                            TradingAutomationModel.instrument_id == candidate.instrument_id,
                            TradingAutomationModel.closed_at.is_not(None),
                        )
                    )
                )
                while str(candidate.automation_id) in closed_ids:
                    candidate = candidate.after_closed_automation()
                session.add(
                    TradingAutomationModel(
                        id=str(candidate.automation_id),
                        user_broker_id=candidate.user_broker_id,
                        instrument_id=candidate.instrument_id,
                        state="HOLD",
                        suspended_from_state=None,
                        hold_reason="BOOTSTRAPPING",
                        revision=1,
                        last_sequence_number=0,
                        resume_requested=False,
                        closed_at=None,
                        bootstrap_position_cycle_id=str(candidate.position_cycle_id),
                        bootstrap_position_lot_id=str(candidate.position_lot_id),
                        bootstrap_quantity_lots=candidate.quantity_lots,
                        bootstrap_average_price=candidate.average_price,
                        bootstrap_invested_amount=candidate.invested_amount,
                        bootstrap_currency=candidate.currency,
                        bootstrap_observed_at=candidate.observed_at,
                        created_at=candidate.observed_at,
                        updated_at=candidate.observed_at,
                    )
                )
                session.flush()
                return PositionAdoptionWriteResult.ADOPTED
        except IntegrityError:
            with self._factory() as session:
                existing = self._active(session, candidate)
                if existing is None:
                    raise PositionAdoptionConflictError("Position adoption constraint failed.") from None
                return self._compare(existing, candidate)

    @staticmethod
    def _active(session: Session, candidate: PositionAdoptionCandidate) -> TradingAutomationModel | None:
        return session.scalar(
            select(TradingAutomationModel).where(
                TradingAutomationModel.user_broker_id == candidate.user_broker_id,
                TradingAutomationModel.instrument_id == candidate.instrument_id,
                TradingAutomationModel.closed_at.is_(None),
            )
        )

    @staticmethod
    def _compare(
        model: TradingAutomationModel,
        candidate: PositionAdoptionCandidate,
    ) -> PositionAdoptionWriteResult:
        if model.bootstrap_position_cycle_id is None:
            return PositionAdoptionWriteResult.EXISTING
        actual = (
            model.bootstrap_quantity_lots,
            model.bootstrap_average_price,
            model.bootstrap_invested_amount,
            model.bootstrap_currency,
        )
        expected = (
            candidate.quantity_lots,
            candidate.average_price,
            candidate.invested_amount,
            candidate.currency,
        )
        if actual != expected:
            raise PositionAdoptionConflictError("Bootstrap snapshot conflicts with active automation.")
        return PositionAdoptionWriteResult.EXISTING
