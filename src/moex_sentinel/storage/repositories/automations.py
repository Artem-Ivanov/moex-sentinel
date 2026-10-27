"""Baseline repository for public trading-automation lifecycle actions."""

from decimal import ROUND_DOWN, Decimal, localcontext
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
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeExecutionModel
from moex_sentinel.storage.models.position_facts import ExecutionLotAllocationModel, PositionLotModel
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
    financial: tuple[str, Decimal, Decimal, Decimal, Decimal],
) -> AutomationRecord:
    financial_status, realized_pnl, unrealized_pnl, net_pnl, actual_commissions = financial
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
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        net_pnl=net_pnl,
        financial_status=financial_status,
        actual_commissions=actual_commissions,
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


_MONEY_QUANTUM = Decimal("0.000000001")


def _allocate_by_lots(amount: Decimal, weights: list[int]) -> list[Decimal]:
    if not weights or any(weight <= 0 for weight in weights):
        return []
    total = sum(weights)
    shares: list[Decimal] = []
    remaining = amount
    with localcontext() as context:
        context.prec = 40
        for weight in weights[:-1]:
            share = (amount * weight / total).quantize(_MONEY_QUANTUM, rounding=ROUND_DOWN)
            shares.append(share)
            remaining -= share
    shares.append(remaining)
    return shares


def _financial_projection(
    session: Session,
    automation: TradingAutomationModel,
    broker: UserBrokerModel,
    cycle: PositionCycleModel | None,
) -> tuple[str, Decimal, Decimal, Decimal, Decimal]:
    """Reconcile raw cycle totals with trusted fees across every cycle and lot."""
    if broker.environment != "PROD":
        return (
            "COMPLETE",
            Decimal() if cycle is None else cycle.realized_pnl,
            Decimal() if cycle is None else cycle.unrealized_pnl,
            Decimal() if cycle is None else cycle.net_pnl,
            Decimal() if cycle is None else cycle.accumulated_commissions,
        )
    projections = session.info.get("broker_fee_automation_projection")
    if projections is None:
        cycles = session.execute(
            select(PositionCycleModel)
            .join(UserBrokerModel, UserBrokerModel.id == PositionCycleModel.user_broker_id)
            .where(UserBrokerModel.environment == "PROD")
        ).scalars().all()
        totals: dict[str, dict[str, Decimal | str | bool]] = {}
        for item in cycles:
            values = totals.setdefault(
                item.automation_id,
                {
                    "status": "COMPLETE",
                    "realized": Decimal(),
                    "unrealized": Decimal(),
                    "net": Decimal(),
                    "commissions": Decimal(),
                    "delta": Decimal(),
                    "unallocated": False,
                },
            )
            values["realized"] += item.realized_pnl
            values["net"] += item.net_pnl
            values["commissions"] += item.accumulated_commissions
            if item.closed_at is None:
                values["unrealized"] += item.unrealized_pnl

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
        fee_rows = session.execute(
            select(BrokerOrderModel, BrokerInstrumentModel, BrokerFeeScanStateModel, BrokerOrderFeeObservationModel, previous_known)
            .join(UserBrokerModel, UserBrokerModel.id == BrokerOrderModel.user_broker_id)
            .join(BrokerInstrumentModel, BrokerInstrumentModel.id == BrokerOrderModel.instrument_id)
            .outerjoin(BrokerFeeScanStateModel, BrokerFeeScanStateModel.user_broker_id == BrokerOrderModel.user_broker_id)
            .outerjoin(
                BrokerOrderFeeObservationModel,
                and_(
                    BrokerOrderFeeObservationModel.user_broker_id == BrokerOrderModel.user_broker_id,
                    BrokerOrderFeeObservationModel.parent_operation_id == BrokerOrderModel.external_order_id,
                    BrokerOrderFeeObservationModel.operation_type == "BROKER_FEE",
                    BrokerOrderFeeObservationModel.currency == BrokerInstrumentModel.currency,
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
        fees_by_order: dict[str, dict[str, object]] = {}
        for order, instrument, scan, observation, previous in fee_rows:
            record = fees_by_order.setdefault(
                order.id,
                {"order": order, "currency": instrument.currency, "scan": scan, "previous": previous, "observations": []},
            )
            if observation is not None:
                record["observations"].append(observation)

        executions = session.execute(
            select(TradeExecutionModel).join(UserBrokerModel, UserBrokerModel.id == TradeExecutionModel.user_broker_id)
            .where(UserBrokerModel.environment == "PROD")
        ).scalars().all()
        executions_by_order: dict[str, list[TradeExecutionModel]] = {}
        for execution in executions:
            executions_by_order.setdefault(execution.broker_order_id, []).append(execution)
        lots = session.execute(
            select(PositionLotModel, TradeExecutionModel.broker_order_id)
            .join(TradeExecutionModel, TradeExecutionModel.id == PositionLotModel.buy_execution_id)
            .join(UserBrokerModel, UserBrokerModel.id == PositionLotModel.user_broker_id)
            .where(UserBrokerModel.environment == "PROD")
        ).all()
        lots_by_order: dict[str, list[PositionLotModel]] = {}
        for lot, order_id in lots:
            lots_by_order.setdefault(order_id, []).append(lot)
        allocation_rows = session.execute(
            select(ExecutionLotAllocationModel, PositionLotModel, TradeExecutionModel.broker_order_id)
            .join(PositionLotModel, PositionLotModel.id == ExecutionLotAllocationModel.position_lot_id)
            .join(TradeExecutionModel, TradeExecutionModel.id == ExecutionLotAllocationModel.sell_execution_id)
            .join(UserBrokerModel, UserBrokerModel.id == ExecutionLotAllocationModel.user_broker_id)
            .where(UserBrokerModel.environment == "PROD")
        ).all()
        allocations_by_lot: dict[str, list[ExecutionLotAllocationModel]] = {}
        allocations_by_sell_order: dict[str, list[ExecutionLotAllocationModel]] = {}
        for allocation, lot, sell_order_id in allocation_rows:
            allocations_by_lot.setdefault(lot.id, []).append(allocation)
            allocations_by_sell_order.setdefault(sell_order_id, []).append(allocation)

        for order_id, fee_record in fees_by_order.items():
            order = fee_record["order"]
            automation_totals = totals.get(order.automation_id)
            if automation_totals is None:
                continue
            observations = fee_record["observations"]
            previous = fee_record["previous"]
            scan = fee_record["scan"]
            known_row = observations[0] if len(observations) == 1 else None
            if (
                known_row is not None
                and known_row.status == "KNOWN"
                and known_row.amount >= order.executed_commission
                and (previous is None or known_row.amount >= previous)
            ):
                effective_fee = known_row.amount
                status = "KNOWN"
                if scan is not None and scan.status == "ERROR":
                    status = "UNKNOWN"
            else:
                effective_fee = max(order.executed_commission, previous or Decimal())
                status = "UNKNOWN" if observations or (scan is not None and scan.status == "ERROR") else "INCOMPLETE"
            if status == "UNKNOWN":
                automation_totals["status"] = "UNKNOWN"
            elif status == "INCOMPLETE" and automation_totals["status"] != "UNKNOWN":
                automation_totals["status"] = "INCOMPLETE"
            delta = effective_fee - order.executed_commission
            if delta <= 0:
                continue
            automation_totals["delta"] += delta
            if order.side == "BUY":
                order_lots = lots_by_order.get(order_id, [])
                total_lots = sum(lot.original_lots for lot in order_lots)
                if total_lots != sum(executed.executed_lots for executed in executions_by_order.get(order_id, [])):
                    automation_totals["unallocated"] = True
                    continue
                targets: list[tuple[str, int]] = []
                coverage_complete = bool(order_lots)
                for lot in sorted(order_lots, key=lambda item: item.id):
                    allocations = allocations_by_lot.get(lot.id, [])
                    allocated = sum(item.allocated_lots for item in allocations)
                    if allocated + lot.remaining_lots != lot.original_lots:
                        coverage_complete = False
                        break
                    targets.extend(("closed", item.allocated_lots) for item in sorted(allocations, key=lambda item: item.id))
                    if lot.remaining_lots:
                        targets.append(("open", lot.remaining_lots))
                if not coverage_complete or sum(weight for _, weight in targets) != total_lots:
                    automation_totals["unallocated"] = True
                    continue
                for (kind, _weight), amount in zip(
                    targets,
                    _allocate_by_lots(delta, [weight for _, weight in targets]),
                    strict=True,
                ):
                    key = "realized_delta" if kind == "closed" else "unrealized_delta"
                    automation_totals[key] = automation_totals.get(key, Decimal()) + amount
            elif order.side == "SELL":
                order_executions = executions_by_order.get(order_id, [])
                allocations = sorted(allocations_by_sell_order.get(order_id, []), key=lambda item: item.id)
                executed_lots = sum(execution.executed_lots for execution in order_executions)
                allocated_lots = sum(item.allocated_lots for item in allocations)
                if not order_executions or allocated_lots != executed_lots:
                    automation_totals["unallocated"] = True
                    continue
                automation_totals["realized_delta"] = automation_totals.get("realized_delta", Decimal()) + delta
            else:
                automation_totals["unallocated"] = True

        projections = {}
        for automation_id, values in totals.items():
            status = values["status"]
            if values["unallocated"]:
                status = "UNKNOWN"
            projections[automation_id] = (
                status,
                values["realized"] - values.get("realized_delta", Decimal()),
                values["unrealized"] - values.get("unrealized_delta", Decimal()),
                values["net"] - values["delta"],
                values["commissions"] + values["delta"],
            )
        session.info["broker_fee_automation_projection"] = projections

    result = projections.get(automation.id)
    if result is not None:
        return result
    if cycle is None:
        return "COMPLETE", Decimal(), Decimal(), Decimal(), Decimal()
    return "INCOMPLETE", cycle.realized_pnl, cycle.unrealized_pnl, cycle.net_pnl, cycle.accumulated_commissions


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
