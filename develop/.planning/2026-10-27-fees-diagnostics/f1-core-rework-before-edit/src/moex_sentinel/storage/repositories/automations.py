"""Baseline repository for public trading-automation lifecycle actions."""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.persistence_errors import RevisionConflictError
from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.order_facts import BrokerOrderModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories import DuplicateRecordError, RecordNotFoundError
from sentinel_contracts.broker_order_policy import BROKER_TERMINAL_STATES
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.trading import AutomationState


def _record(
    automation: TradingAutomationModel,
    broker: UserBrokerModel,
    instrument: BrokerInstrumentModel,
    cycle: PositionCycleModel | None,
    financial: tuple[str, Decimal, Decimal, Decimal],
) -> AutomationRecord:
    financial_status, effective_net_pnl, effective_commissions, effective_broker_commission = financial
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
        hold_reason=automation.hold_reason,
        order_quota_percent=automation.order_quota_percent,
        settings_revision=automation.settings_revision,
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
        financial_status=financial_status,
        effective_net_pnl=effective_net_pnl,
        effective_actual_commissions=effective_commissions,
        effective_broker_commission=effective_broker_commission,
        broker_name=broker.display_name,
        ticker=instrument.ticker,
        instrument_name=instrument.name,
        bootstrap_pending=(
            automation.state in {AutomationState.HOLD.value, AutomationState.IN_QUEUE.value}
            and automation.last_sequence_number == 0
            and all(
                value is not None
                for value in (
                    automation.bootstrap_position_cycle_id,
                    automation.bootstrap_position_lot_id,
                    automation.bootstrap_quantity_lots,
                    automation.bootstrap_average_price,
                    automation.bootstrap_invested_amount,
                    automation.bootstrap_currency,
                    automation.bootstrap_observed_at,
                )
            )
        ),
    )


def _financial_projection(
    session: Session,
    automation: TradingAutomationModel,
    broker: UserBrokerModel,
    cycle: PositionCycleModel | None,
) -> tuple[str, Decimal, Decimal, Decimal]:
    """Project only confirmed order-fee deltas on the current cycle's immutable totals."""
    raw_pnl = Decimal() if cycle is None else cycle.net_pnl
    raw_commissions = Decimal() if cycle is None else cycle.accumulated_commissions
    if cycle is None:
        return "COMPLETE", raw_pnl, raw_commissions, Decimal()
    if broker.environment != "PROD":
        return "COMPLETE", raw_pnl, raw_commissions, Decimal()
    projections = session.info.get("broker_fee_cycle_projection")
    if projections is None:
        previous_known = (
            select(func.max(BrokerOrderFeeObservationModel.amount))
            .where(
                BrokerOrderFeeObservationModel.user_broker_id == BrokerOrderModel.user_broker_id,
                BrokerOrderFeeObservationModel.parent_operation_id == BrokerOrderModel.external_order_id,
                BrokerOrderFeeObservationModel.currency == BrokerInstrumentModel.currency,
                BrokerOrderFeeObservationModel.status == "KNOWN",
                BrokerOrderFeeObservationModel.scan_pass_id != BrokerFeeScanStateModel.completed_scan_pass_id,
            )
            .correlate(BrokerOrderModel, BrokerFeeScanStateModel, BrokerInstrumentModel)
            .scalar_subquery()
        )
        rows = session.execute(
            select(
                BrokerOrderModel,
                BrokerFeeScanStateModel,
                BrokerOrderFeeObservationModel,
                previous_known,
            )
            .join(UserBrokerModel, UserBrokerModel.id == BrokerOrderModel.user_broker_id)
            .join(BrokerInstrumentModel, BrokerInstrumentModel.id == BrokerOrderModel.instrument_id)
            .outerjoin(
                BrokerFeeScanStateModel,
                BrokerFeeScanStateModel.user_broker_id == BrokerOrderModel.user_broker_id,
            )
            .outerjoin(
                BrokerOrderFeeObservationModel,
                and_(
                    BrokerOrderFeeObservationModel.user_broker_id == BrokerOrderModel.user_broker_id,
                    BrokerOrderFeeObservationModel.broker_order_id == BrokerOrderModel.id,
                    BrokerOrderFeeObservationModel.scan_pass_id == BrokerFeeScanStateModel.completed_scan_pass_id,
                ),
            )
            .where(
                UserBrokerModel.environment == "PROD",
                BrokerOrderModel.state.in_(tuple(state.value for state in BROKER_TERMINAL_STATES)),
                BrokerOrderModel.terminal_at.is_not(None),
                BrokerOrderModel.executed_amount > 0,
            )
        ).all()
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        for order, scan, observation, previous in rows:
            key = (order.automation_id, order.position_cycle_id or "")
            group = grouped.setdefault(key, {"orders": {}, "incomplete": False, "unknown": False})
            orders = group["orders"]
            order_data = orders.setdefault(order.id, {"raw": order.executed_commission, "observations": []})
            if scan is None or not scan.completed_scan_pass_id or observation is None:
                group["incomplete"] = True
            else:
                order_data["observations"].append((observation, previous))
        projections = {}
        for key, group in grouped.items():
            delta = Decimal()
            reported = Decimal()
            for order_data in group["orders"].values():
                observations = order_data["observations"]
                if len(observations) != 1:
                    if any(item.status == "UNKNOWN" for item, _previous in observations):
                        group["unknown"] = True
                    else:
                        group["incomplete"] = True
                    continue
                observation, previous = observations[0]
                if observation.status == "KNOWN":
                    reported += observation.amount
                    delta += observation.amount - order_data["raw"]
                elif observation.status == "UNKNOWN":
                    group["unknown"] = True
                    if previous is not None:
                        reported += previous
                        delta += previous - order_data["raw"]
                else:
                    group["incomplete"] = True
            status = "UNKNOWN" if group["unknown"] else "INCOMPLETE" if group["incomplete"] else "COMPLETE"
            projections[key] = (status, delta, reported)
        session.info["broker_fee_cycle_projection"] = projections
    projection = projections.get((automation.id, cycle.id))
    if projection is None:
        status = "COMPLETE" if raw_commissions == 0 else "INCOMPLETE"
        return status, raw_pnl, raw_commissions, Decimal()
    status, delta, reported = projection
    return status, raw_pnl - delta, raw_commissions + delta, reported


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
                return _record(
                    automation,
                    broker,
                    instrument,
                    None,
                    _financial_projection(session, automation, broker, None),
                )
        except IntegrityError:
            raise DuplicateRecordError("Active automation already exists.") from None

    def get(self, automation_id: str) -> AutomationRecord:
        with self._factory() as session:
            return self._get_record(session, automation_id)

    def is_current_worker_technical_hold(self, automation_id: str) -> bool:
        with self._factory() as session:
            automation = self._get_model(session, automation_id)
            if (
                automation.state != AutomationState.HOLD.value
                or automation.suspended_from_state != AutomationState.IN_WORK.value
                or automation.hold_reason != "Uncertain broker intent could not be reconciled unambiguously"
            ):
                return False
            event = session.scalar(
                select(AutomationEventModel)
                .where(
                    AutomationEventModel.user_broker_id == automation.user_broker_id,
                    AutomationEventModel.automation_id == automation.id,
                    AutomationEventModel.fact_kind == "AUTOMATION_STATE_CHANGED",
                )
                .order_by(AutomationEventModel.sequence_number.desc())
                .limit(1)
            )
            return bool(
                event is not None
                and event.expected_revision + 1 == automation.revision
                and event.payload.get("state") == "HOLD"
                and event.payload.get("suspended_from_state") == "IN_WORK"
                and event.payload.get("hold_reason") == automation.hold_reason
            )

    def get_many(self, automation_ids: list[str]) -> list[AutomationRecord]:
        requested = tuple(dict.fromkeys(automation_ids))
        if not requested:
            return []
        with self._factory() as session:
            return self._list_records(session, TradingAutomationModel.id.in_(requested))

    def list_active(self) -> list[AutomationRecord]:
        with self._factory() as session:
            return self._list_records(session, TradingAutomationModel.closed_at.is_(None))

    @staticmethod
    def _list_records(session: Session, condition: ColumnElement[bool]) -> list[AutomationRecord]:
        statement = (
            select(TradingAutomationModel, UserBrokerModel, BrokerInstrumentModel, PositionCycleModel)
            .select_from(TradingAutomationModel)
            .outerjoin(UserBrokerModel, UserBrokerModel.id == TradingAutomationModel.user_broker_id)
            .outerjoin(
                BrokerInstrumentModel,
                and_(
                    BrokerInstrumentModel.id == TradingAutomationModel.instrument_id,
                    BrokerInstrumentModel.user_broker_id == TradingAutomationModel.user_broker_id,
                ),
            )
            .outerjoin(
                PositionCycleModel,
                and_(
                    PositionCycleModel.user_broker_id == TradingAutomationModel.user_broker_id,
                    PositionCycleModel.automation_id == TradingAutomationModel.id,
                    PositionCycleModel.closed_at.is_(None),
                ),
            )
            .where(condition)
            .order_by(TradingAutomationModel.created_at, TradingAutomationModel.id)
        )
        records = []
        for automation, broker, instrument, cycle in session.execute(statement):
            if broker is None or instrument is None:
                raise RecordNotFoundError("Automation scope is incomplete.")
            records.append(
                _record(
                    automation,
                    broker,
                    instrument,
                    cycle,
                    _financial_projection(session, automation, broker, cycle),
                )
            )
        return records

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
            suspended_from_state = (
                model.suspended_from_state if model.state == AutomationState.HOLD.value else model.state
            )
            result = session.execute(
                update(TradingAutomationModel)
                .where(
                    TradingAutomationModel.id == automation_id,
                    TradingAutomationModel.revision == expected_revision,
                )
                .values(
                    state=state.value,
                    suspended_from_state=suspended_from_state if state is AutomationState.HOLD else None,
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

    def update_settings(
        self,
        automation_id: str,
        *,
        expected_revision: int,
        expected_settings_revision: int,
        order_quota_percent: Decimal | None,
    ) -> AutomationRecord:
        with self._factory.begin() as session:
            model = self._get_model(session, automation_id)
            now = utc_now_ms()
            result = session.execute(
                update(TradingAutomationModel)
                .where(
                    TradingAutomationModel.id == automation_id,
                    TradingAutomationModel.revision == expected_revision,
                    TradingAutomationModel.settings_revision == expected_settings_revision,
                    TradingAutomationModel.closed_at.is_(None),
                    TradingAutomationModel.state != AutomationState.CLOSED.value,
                )
                .values(
                    order_quota_percent=order_quota_percent,
                    settings_revision=expected_settings_revision + 1,
                    updated_at=now,
                )
                .returning(TradingAutomationModel.id)
            )
            if result.scalar_one_or_none() is None:
                if model.closed_at is not None:
                    raise RevisionConflictError("Closed automation settings cannot be changed.")
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
        return _record(model, broker, instrument, cycle, _financial_projection(session, model, broker, cycle))
