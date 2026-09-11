"""Native strategy-free Worker command projection from baseline Core tables."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatus,
    AutomationStatusesResult,
    BrokerPositionBootstrap,
)


class AutomationCommandRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def claim(self, limit: int) -> list[AutomationCommand]:
        if limit <= 0:
            return []
        with self._factory() as session:
            rows = session.execute(
                select(TradingAutomationModel, UserBrokerModel, BrokerInstrumentModel)
                .join(UserBrokerModel, UserBrokerModel.id == TradingAutomationModel.user_broker_id)
                .join(
                    BrokerInstrumentModel,
                    (BrokerInstrumentModel.user_broker_id == TradingAutomationModel.user_broker_id)
                    & (BrokerInstrumentModel.id == TradingAutomationModel.instrument_id),
                )
                .where(
                    UserBrokerModel.state == "ACTIVE",
                    UserBrokerModel.external_account_id.is_not(None),
                    or_(
                        TradingAutomationModel.state == AutomationState.IN_QUEUE.value,
                        and_(
                            TradingAutomationModel.state == AutomationState.HOLD.value,
                            TradingAutomationModel.last_sequence_number == 0,
                            TradingAutomationModel.bootstrap_position_cycle_id.is_not(None),
                        ),
                        TradingAutomationModel.resume_requested.is_(True),
                    ),
                )
                .order_by(TradingAutomationModel.created_at, TradingAutomationModel.id)
                .limit(limit)
            ).all()
        return [self._command(automation, user_broker, instrument) for automation, user_broker, instrument in rows]

    def statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
        requested = tuple(dict.fromkeys(automation_ids))
        if not requested:
            return AutomationStatusesResult(automations=())
        by_text = {str(value): value for value in requested}
        with self._factory() as session:
            rows = session.scalars(
                select(TradingAutomationModel).where(TradingAutomationModel.id.in_(tuple(by_text)))
            ).all()
        by_id = {row.id: row for row in rows}
        return AutomationStatusesResult(
            automations=tuple(
                AutomationStatus(
                    automation_id=automation_id,
                    user_broker_id=UUID(by_id[str(automation_id)].user_broker_id),
                    state=AutomationState(by_id[str(automation_id)].state),
                    revision=by_id[str(automation_id)].revision,
                    last_sequence_number=by_id[str(automation_id)].last_sequence_number,
                    resume_requested=by_id[str(automation_id)].resume_requested,
                )
                for automation_id in requested
                if str(automation_id) in by_id
            ),
            missing_automation_ids=tuple(value for value in requested if str(value) not in by_id),
        )

    @staticmethod
    def _command(
        automation: TradingAutomationModel,
        user_broker: UserBrokerModel,
        instrument: BrokerInstrumentModel,
    ) -> AutomationCommand:
        if user_broker.external_account_id is None:
            raise RuntimeError("Active user broker has no external account.")
        scope_id = UUID(user_broker.id)
        bootstrap = None
        if (
            automation.bootstrap_position_cycle_id is not None
            and automation.state == AutomationState.HOLD.value
            and automation.last_sequence_number == 0
        ):
            required = (
                automation.bootstrap_position_lot_id,
                automation.bootstrap_quantity_lots,
                automation.bootstrap_average_price,
                automation.bootstrap_invested_amount,
                automation.bootstrap_currency,
                automation.bootstrap_observed_at,
            )
            if any(value is None for value in required):
                raise RuntimeError("Automation bootstrap snapshot is incomplete.")
            bootstrap = BrokerPositionBootstrap(
                position_cycle_id=UUID(automation.bootstrap_position_cycle_id),
                position_lot_id=UUID(automation.bootstrap_position_lot_id),
                quantity_lots=automation.bootstrap_quantity_lots,
                average_price=automation.bootstrap_average_price,
                invested_amount=automation.bootstrap_invested_amount,
                currency=automation.bootstrap_currency,
                observed_at=automation.bootstrap_observed_at,
            )
        return AutomationCommand(
            automation_id=UUID(automation.id),
            user_broker_id=scope_id,
            broker_id=scope_id,
            account_id=user_broker.external_account_id,
            external_instrument_id=instrument.external_instrument_id,
            instrument_id=UUID(instrument.id),
            currency=instrument.currency,
            lot_size=instrument.lot_size,
            min_price_increment=instrument.min_price_increment,
            state=AutomationState(automation.state),
            revision=automation.revision,
            last_sequence_number=automation.last_sequence_number,
            resume_requested=automation.resume_requested,
            bootstrap=bootstrap,
        )
