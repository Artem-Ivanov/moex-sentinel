"""Session-bound automation repository."""

from datetime import datetime
from typing import NoReturn

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.repositories.trading_facts_support import flush_or_translate, require_scoped_reference
from sentinel_contracts.trading import AutomationState


def _automation_value(model: TradingAutomationModel) -> TradingAutomationDraft:
    return TradingAutomationDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        instrument_id=model.instrument_id,
        state=AutomationState(model.state),
        suspended_from_state=AutomationState(model.suspended_from_state) if model.suspended_from_state else None,
        hold_reason=model.hold_reason,
        revision=model.revision,
        last_sequence_number=model.last_sequence_number,
        resume_requested=model.resume_requested,
        created_at=model.created_at,
        updated_at=model.updated_at,
        closed_at=model.closed_at,
        bootstrap_position_cycle_id=model.bootstrap_position_cycle_id,
        bootstrap_position_lot_id=model.bootstrap_position_lot_id,
        bootstrap_quantity_lots=model.bootstrap_quantity_lots,
        bootstrap_average_price=model.bootstrap_average_price,
        bootstrap_invested_amount=model.bootstrap_invested_amount,
        bootstrap_currency=model.bootstrap_currency,
        bootstrap_observed_at=model.bootstrap_observed_at,
    )


class AutomationFactsRepository:
    """Persist the automation aggregate without owning its transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        user_broker_id: str,
        automation: TradingAutomationDraft,
    ) -> TradingAutomationDraft:
        if automation.user_broker_id != user_broker_id:
            raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type="automation")
        require_scoped_reference(
            self._session,
            scope_column=BrokerInstrumentModel.user_broker_id,
            id_column=BrokerInstrumentModel.id,
            user_broker_id=user_broker_id,
            reference_id=automation.instrument_id,
            entity_type="automation",
        )
        automation_model = TradingAutomationModel(**automation.model_dump(mode="python"))
        self._session.add(automation_model)
        flush_or_translate(self._session, entity_type="automation")
        return _automation_value(automation_model)

    def get(self, user_broker_id: str, automation_id: str) -> TradingAutomationDraft:
        model = self._session.scalar(
            select(TradingAutomationModel).where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
            )
        )
        if model is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="automation")
        return _automation_value(model)

    def compare_and_set_state(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        state: AutomationState,
        hold_reason: str | None,
        closed_at: datetime | None,
    ) -> TradingAutomationDraft:
        result = self._session.execute(
            update(TradingAutomationModel)
            .where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
                TradingAutomationModel.revision == expected_revision,
            )
            .values(
                state=state.value,
                hold_reason=hold_reason,
                closed_at=closed_at,
                revision=expected_revision + 1,
            )
            .returning(TradingAutomationModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.REVISION_CONFLICT, entity_type="automation")
        flush_or_translate(self._session, entity_type="automation")
        return self.get(user_broker_id, automation_id)

    def accept_state_fact(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        expected_sequence: int,
        sequence_number: int,
        state: AutomationState,
        suspended_from_state: AutomationState | None,
        hold_reason: str | None,
        closed_at: datetime | None,
    ) -> TradingAutomationDraft:
        self._require_next_sequence(expected_sequence, sequence_number)
        result = self._session.execute(
            update(TradingAutomationModel)
            .where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
                TradingAutomationModel.revision == expected_revision,
                TradingAutomationModel.last_sequence_number == expected_sequence,
            )
            .values(
                state=state.value,
                suspended_from_state=suspended_from_state.value if suspended_from_state else None,
                hold_reason=hold_reason,
                closed_at=closed_at,
                revision=expected_revision + 1,
                last_sequence_number=sequence_number,
            )
            .returning(TradingAutomationModel)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        model = result.scalar_one_or_none()
        if model is None:
            self._raise_accept_conflict(user_broker_id, automation_id, expected_revision)
        flush_or_translate(self._session, entity_type="automation")
        return _automation_value(model)

    def accept_supporting_fact(
        self,
        user_broker_id: str,
        automation_id: str,
        *,
        expected_revision: int,
        expected_sequence: int,
        sequence_number: int,
    ) -> TradingAutomationDraft:
        self._require_next_sequence(expected_sequence, sequence_number)
        result = self._session.execute(
            update(TradingAutomationModel)
            .where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
                TradingAutomationModel.revision == expected_revision,
                TradingAutomationModel.last_sequence_number == expected_sequence,
            )
            .values(last_sequence_number=sequence_number)
            .returning(TradingAutomationModel)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        model = result.scalar_one_or_none()
        if model is None:
            self._raise_accept_conflict(user_broker_id, automation_id, expected_revision)
        flush_or_translate(self._session, entity_type="automation")
        return _automation_value(model)

    @staticmethod
    def _require_next_sequence(expected_sequence: int, sequence_number: int) -> None:
        if sequence_number != expected_sequence + 1:
            raise TradingFactPersistenceError(TradingFactErrorCode.SEQUENCE_CONFLICT, entity_type="automation")

    def _raise_accept_conflict(
        self,
        user_broker_id: str,
        automation_id: str,
        expected_revision: int,
    ) -> NoReturn:
        current_revision = self._session.scalar(
            select(TradingAutomationModel.revision).where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
            )
        )
        if current_revision is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="automation")
        code = (
            TradingFactErrorCode.REVISION_CONFLICT
            if current_revision != expected_revision
            else TradingFactErrorCode.SEQUENCE_CONFLICT
        )
        raise TradingFactPersistenceError(code, entity_type="automation")
