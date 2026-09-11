"""Baseline repository for public trading-automation lifecycle actions."""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories import DuplicateRecordError, RecordNotFoundError
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.trading import AutomationState


class RevisionConflictError(ValueError):
    pass


def _record(
    automation: TradingAutomationModel,
    broker: UserBrokerModel,
    instrument: BrokerInstrumentModel,
    cycle: PositionCycleModel | None,
) -> AutomationRecord:
    return AutomationRecord(
        id=automation.id,
        broker_id=broker.id,
        account_id=broker.external_account_id or "",
        instrument_id=instrument.id,
        state=AutomationState(automation.state),
        suspended_from_state=(
            AutomationState(automation.suspended_from_state) if automation.suspended_from_state else None
        ),
        revision=automation.revision,
        last_sequence_number=automation.last_sequence_number,
        resume_requested=automation.resume_requested,
        currency=instrument.currency,
        quantity_lots=0 if cycle is None else cycle.quantity_lots,
        average_price=Decimal() if cycle is None else cycle.average_entry_price,
        invested_amount=Decimal() if cycle is None else cycle.invested_amount,
        realized_pnl=Decimal() if cycle is None else cycle.realized_pnl,
        unrealized_pnl=Decimal() if cycle is None else cycle.unrealized_pnl,
        net_pnl=Decimal() if cycle is None else cycle.net_pnl,
        actual_commissions=Decimal() if cycle is None else cycle.accumulated_commissions,
        broker_name=broker.display_name,
        ticker=instrument.ticker,
        instrument_name=instrument.name,
    )


class AutomationRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def create(self, *, broker_id: str, account_id: str, instrument_id: str) -> AutomationRecord:
        now = utc_now_ms()
        try:
            with self._factory.begin() as session:
                broker = session.get(UserBrokerModel, broker_id)
                if broker is None or broker.state != "ACTIVE" or broker.external_account_id != account_id:
                    raise RecordNotFoundError("Active user broker account was not found.")
                instrument = session.scalar(
                    select(BrokerInstrumentModel).where(
                        BrokerInstrumentModel.user_broker_id == broker_id,
                        BrokerInstrumentModel.id == instrument_id,
                        BrokerInstrumentModel.is_active.is_(True),
                    )
                )
                if instrument is None:
                    raise RecordNotFoundError("Broker instrument was not found.")
                automation = TradingAutomationModel(
                    id=str(uuid4()),
                    user_broker_id=broker_id,
                    instrument_id=instrument_id,
                    state=AutomationState.IN_QUEUE.value,
                    suspended_from_state=None,
                    hold_reason=None,
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    closed_at=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(automation)
                session.flush()
                return _record(automation, broker, instrument, None)
        except IntegrityError:
            raise DuplicateRecordError("Active automation already exists.") from None

    def get(self, automation_id: str) -> AutomationRecord:
        with self._factory() as session:
            return self._get_record(session, automation_id)

    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]:
        requested = tuple(dict.fromkeys(automation_ids))
        if not requested:
            return []
        with self._factory() as session:
            return [
                self._record_for_model(session, model)
                for model in session.scalars(
                    select(TradingAutomationModel)
                    .where(TradingAutomationModel.id.in_(requested))
                    .order_by(TradingAutomationModel.created_at, TradingAutomationModel.id)
                )
            ]

    def list_active(self) -> list[AutomationRecord]:
        with self._factory() as session:
            return [
                self._record_for_model(session, model)
                for model in session.scalars(
                    select(TradingAutomationModel)
                    .where(TradingAutomationModel.closed_at.is_(None))
                    .order_by(TradingAutomationModel.created_at, TradingAutomationModel.id)
                )
            ]

    def set_state(
        self,
        automation_id: str,
        state: AutomationState,
        *,
        expected_revision: int,
        hold_reason: str | None = None,
    ) -> AutomationRecord:
        with self._factory.begin() as session:
            model = self._get_model(session, automation_id)
            if model.revision != expected_revision:
                raise RevisionConflictError("Automation revision has changed.")
            now = utc_now_ms()
            result = session.execute(
                update(TradingAutomationModel)
                .where(
                    TradingAutomationModel.id == automation_id,
                    TradingAutomationModel.revision == expected_revision,
                )
                .values(
                    state=state.value,
                    suspended_from_state=model.state if state is AutomationState.HOLD else None,
                    hold_reason=hold_reason if state is AutomationState.HOLD else None,
                    resume_requested=False,
                    closed_at=now if state is AutomationState.CLOSED else None,
                    revision=expected_revision + 1,
                    updated_at=now,
                )
                .returning(TradingAutomationModel.id)
            )
            if result.scalar_one_or_none() is None:
                raise RevisionConflictError("Automation revision has changed.")
            session.refresh(model)
            return self._record_for_model(session, model)

    def resume_to_queue(self, automation_id: str, *, expected_revision: int) -> AutomationRecord:
        return self.set_state(
            automation_id,
            AutomationState.IN_QUEUE,
            expected_revision=expected_revision,
        )

    def _get_record(self, session: Session, automation_id: str) -> AutomationRecord:
        return self._record_for_model(session, self._get_model(session, automation_id))

    @staticmethod
    def _get_model(session: Session, automation_id: str) -> TradingAutomationModel:
        model = session.get(TradingAutomationModel, automation_id)
        if model is None:
            raise RecordNotFoundError("Automation was not found.")
        return model

    @staticmethod
    def _record_for_model(session: Session, model: TradingAutomationModel) -> AutomationRecord:
        broker = session.get(UserBrokerModel, model.user_broker_id)
        instrument = session.get(BrokerInstrumentModel, model.instrument_id)
        if broker is None or instrument is None:
            raise RecordNotFoundError("Automation scope is incomplete.")
        cycle = session.scalar(
            select(PositionCycleModel).where(
                PositionCycleModel.user_broker_id == model.user_broker_id,
                PositionCycleModel.automation_id == model.id,
                PositionCycleModel.closed_at.is_(None),
            )
        )
        return _record(model, broker, instrument, cycle)
