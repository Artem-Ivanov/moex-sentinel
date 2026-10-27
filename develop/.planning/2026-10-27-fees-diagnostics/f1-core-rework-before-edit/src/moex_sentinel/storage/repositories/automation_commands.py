"""Native strategy-free Worker command projection from baseline Core tables."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.order_facts import BrokerOrderModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationCommissionStatus,
    AutomationStatus,
    AutomationStatusesResult,
    BrokerPositionBootstrap,
)


class AutomationCommandRepository:
    def __init__(self, factory: sessionmaker[Session], *, environment: str | None = None) -> None:
        self._factory = factory
        self._environment = environment

    def claim(self, limit: int, *, broker_id: UUID | None = None) -> list[AutomationCommand]:
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
                    True if self._environment is None else UserBrokerModel.environment == self._environment,
                    UserBrokerModel.external_account_id.is_not(None),
                    True if broker_id is None else UserBrokerModel.id == str(broker_id),
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

    def statuses(
        self,
        automation_ids: list[UUID],
        *,
        broker_id: UUID | None = None,
        commission_order_ids: list[UUID] | None = None,
    ) -> AutomationStatusesResult:
        requested = tuple(dict.fromkeys(automation_ids))
        fee_requested = tuple(dict.fromkeys(commission_order_ids or ()))
        if len(fee_requested) > 100:
            raise ValueError("At most 100 commission order IDs may be requested.")
        if not requested and not fee_requested:
            return AutomationStatusesResult(automations=())
        by_text = {str(value): value for value in requested}
        with self._factory() as session:
            rows = session.scalars(
                select(TradingAutomationModel)
                .join(UserBrokerModel, UserBrokerModel.id == TradingAutomationModel.user_broker_id)
                .where(
                    TradingAutomationModel.id.in_(tuple(by_text)),
                    True if self._environment is None else UserBrokerModel.environment == self._environment,
                    True if broker_id is None else UserBrokerModel.id == str(broker_id),
                )
            ).all()
            fees = []
            for order_id in fee_requested:
                order = session.scalar(
                    select(BrokerOrderModel)
                    .join(UserBrokerModel, UserBrokerModel.id == BrokerOrderModel.user_broker_id)
                    .where(
                        BrokerOrderModel.idempotency_key == str(order_id),
                        True if self._environment is None else UserBrokerModel.environment == self._environment,
                        True if broker_id is None else UserBrokerModel.id == str(broker_id),
                    )
                )
                if order is None:
                    continue
                scan = session.get(BrokerFeeScanStateModel, order.user_broker_id)
                row = None
                if scan is not None and scan.completed_scan_pass_id:
                    row = session.scalar(
                        select(BrokerOrderFeeObservationModel).where(
                            BrokerOrderFeeObservationModel.user_broker_id == order.user_broker_id,
                            BrokerOrderFeeObservationModel.broker_order_id == order.id,
                            BrokerOrderFeeObservationModel.scan_pass_id == scan.completed_scan_pass_id,
                        )
                    )
                if row is None:
                    status, amount = "MISSING", None
                elif row.status == "KNOWN":
                    status, amount = "KNOWN", row.amount
                else:
                    status, amount = "UNKNOWN", None
                instrument = session.get(BrokerInstrumentModel, order.instrument_id)
                if instrument is None:
                    continue
                fees.append(
                    AutomationCommissionStatus(
                        intent_id=order_id,
                        automation_id=UUID(order.automation_id),
                        user_broker_id=UUID(order.user_broker_id),
                        currency=instrument.currency,
                        commission_status=status,
                        effective_broker_commission=amount,
                    )
                )
        by_id = {row.id: row for row in rows}
        return AutomationStatusesResult(
            automations=tuple(
                AutomationStatus(
                    automation_id=automation_id,
                    user_broker_id=UUID(by_id[str(automation_id)].user_broker_id),
                    state=AutomationState(by_id[str(automation_id)].state),
                    revision=by_id[str(automation_id)].revision,
                    settings_revision=by_id[str(automation_id)].settings_revision,
                    last_sequence_number=by_id[str(automation_id)].last_sequence_number,
                    resume_requested=by_id[str(automation_id)].resume_requested,
                    order_quota_percent=by_id[str(automation_id)].order_quota_percent,
                )
                for automation_id in requested
                if str(automation_id) in by_id
            ),
            missing_automation_ids=tuple(value for value in requested if str(value) not in by_id),
            commission_statuses=tuple(fees),
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
            and automation.state in {AutomationState.HOLD.value, AutomationState.IN_QUEUE.value}
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
            settings_revision=automation.settings_revision,
            last_sequence_number=automation.last_sequence_number,
            resume_requested=automation.resume_requested,
            order_quota_percent=automation.order_quota_percent,
            bootstrap=bootstrap,
        )
