"""Session-bound decision, order and execution repository."""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    BrokerOrderStatus,
    BrokerOrderType,
    ExecutionSource,
    OrderIntentKind,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.order_facts import (
    BrokerOrderEventModel,
    BrokerOrderModel,
    TradeDecisionModel,
    TradeExecutionModel,
)
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.repositories.trading_facts_support import (
    append_idempotent,
    flush_or_translate,
    require_scoped_reference,
    translate_database_errors,
)
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import DecisionKind


def _external_order_id(value: str | None) -> str | None:
    return value if value and value.strip() else None


def _decision_value(model: TradeDecisionModel) -> TradeDecisionDraft:
    return TradeDecisionDraft(
        id=model.id,
        fact_id=model.fact_id,
        process_id=model.process_id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        position_cycle_id=model.position_cycle_id,
        instrument_id=model.instrument_id,
        quantity_lots=model.quantity_lots,
        lot_size=model.lot_size,
        average_price=model.average_price,
        invested_amount=model.invested_amount,
        current_price=model.current_price,
        best_bid=model.best_bid,
        best_ask=model.best_ask,
        indicators=dict(model.indicators),
        estimated_commission=model.estimated_commission,
        decision=DecisionKind(model.decision),
        reason_code=model.reason_code,
        requested_quantity_lots=model.requested_quantity_lots,
        limit_price=model.limit_price,
        strategy_snapshot=dict(model.strategy_snapshot),
        decided_at=model.decided_at,
        created_at=model.created_at,
    )


def _order_value(model: BrokerOrderModel) -> BrokerOrderDraft:
    return BrokerOrderDraft(
        id=model.id,
        fact_id=model.fact_id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        decision_id=model.decision_id,
        position_cycle_id=model.position_cycle_id,
        instrument_id=model.instrument_id,
        idempotency_key=model.idempotency_key,
        external_order_id=_external_order_id(model.external_order_id),
        intent_kind=OrderIntentKind(model.intent_kind),
        side=OrderSide(model.side),
        order_type=BrokerOrderType(model.order_type),
        state=BrokerOrderStatus(model.state),
        quantity_lots=model.quantity_lots,
        limit_price=model.limit_price,
        requested_amount=model.requested_amount,
        executed_amount=model.executed_amount,
        estimated_commission=model.estimated_commission,
        executed_commission=model.executed_commission,
        strategy_snapshot=dict(model.strategy_snapshot),
        created_at=model.created_at,
        dispatch_started_at=model.dispatch_started_at,
        broker_responded_at=model.broker_responded_at,
        executed_at=model.executed_at,
        terminal_at=model.terminal_at,
        updated_at=model.updated_at,
    )


def _event_value(model: BrokerOrderEventModel) -> BrokerOrderEventDraft:
    return BrokerOrderEventDraft(
        id=model.id,
        fact_id=model.fact_id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        broker_order_id=model.broker_order_id,
        from_state=BrokerOrderStatus(model.from_state) if model.from_state else None,
        to_state=BrokerOrderStatus(model.to_state),
        safe_reason=model.safe_reason,
        safe_message=model.safe_message,
        occurred_at=model.occurred_at,
        created_at=model.created_at,
    )


def _execution_value(model: TradeExecutionModel) -> TradeExecutionDraft:
    return TradeExecutionDraft(
        id=model.id,
        fact_id=model.fact_id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        broker_order_id=model.broker_order_id,
        position_cycle_id=model.position_cycle_id,
        instrument_id=model.instrument_id,
        external_execution_id=model.external_execution_id,
        side=OrderSide(model.side),
        executed_lots=model.executed_lots,
        price=model.price,
        value=model.value,
        broker_commission=model.broker_commission,
        other_fees=model.other_fees,
        currency=model.currency,
        source=ExecutionSource(model.source),
        executed_at=model.executed_at,
        created_at=model.created_at,
    )


def _require_scope(user_broker_id: str, value_scope: str, entity_type: str) -> None:
    if user_broker_id != value_scope:
        raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)


class OrderFactsRepository:
    """Persist immutable order facts without owning their transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_decision(self, user_broker_id: str, value: TradeDecisionDraft) -> TradeDecisionDraft:
        _require_scope(user_broker_id, value.user_broker_id, "trade_decision")
        self._require_common_lineage(
            user_broker_id,
            automation_id=value.automation_id,
            cycle_id=value.position_cycle_id,
            instrument_id=value.instrument_id,
            entity_type="trade_decision",
        )
        model = TradeDecisionModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=TradeDecisionModel.fact_id == value.fact_id,
            to_value=_decision_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="trade_decision",
        )

    def append_order(self, user_broker_id: str, value: BrokerOrderDraft) -> BrokerOrderDraft:
        _require_scope(user_broker_id, value.user_broker_id, "broker_order")
        self._require_common_lineage(
            user_broker_id,
            automation_id=value.automation_id,
            cycle_id=value.position_cycle_id,
            instrument_id=value.instrument_id,
            entity_type="broker_order",
        )
        require_scoped_reference(
            self._session,
            scope_column=TradeDecisionModel.user_broker_id,
            id_column=TradeDecisionModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.decision_id,
            entity_type="broker_order",
        )
        self._require_decision_lineage(user_broker_id, value)
        model = BrokerOrderModel(
            **{**value.model_dump(mode="python"), "external_order_id": _external_order_id(value.external_order_id)}
        )
        return append_idempotent(
            self._session,
            candidate=model,
            identity=BrokerOrderModel.fact_id == value.fact_id,
            to_value=_order_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="broker_order",
        )

    def append_order_event(self, user_broker_id: str, value: BrokerOrderEventDraft) -> BrokerOrderEventDraft:
        _require_scope(user_broker_id, value.user_broker_id, "broker_order_event")
        require_scoped_reference(
            self._session,
            scope_column=TradingAutomationModel.user_broker_id,
            id_column=TradingAutomationModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.automation_id,
            entity_type="broker_order_event",
        )
        matching_order = self._session.scalar(
            select(BrokerOrderModel.id).where(
                BrokerOrderModel.user_broker_id == user_broker_id,
                BrokerOrderModel.id == value.broker_order_id,
                BrokerOrderModel.automation_id == value.automation_id,
            )
        )
        if matching_order is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="broker_order_event",
            )
        require_scoped_reference(
            self._session,
            scope_column=BrokerOrderModel.user_broker_id,
            id_column=BrokerOrderModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.broker_order_id,
            entity_type="broker_order_event",
        )
        model = BrokerOrderEventModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=BrokerOrderEventModel.fact_id == value.fact_id,
            to_value=_event_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="broker_order_event",
        )

    def append_execution(self, user_broker_id: str, value: TradeExecutionDraft) -> TradeExecutionDraft:
        _require_scope(user_broker_id, value.user_broker_id, "trade_execution")
        self._require_common_lineage(
            user_broker_id,
            automation_id=value.automation_id,
            cycle_id=value.position_cycle_id,
            instrument_id=value.instrument_id,
            entity_type="trade_execution",
        )
        matching_order = self._session.scalar(
            select(BrokerOrderModel.id).where(
                BrokerOrderModel.user_broker_id == user_broker_id,
                BrokerOrderModel.id == value.broker_order_id,
                BrokerOrderModel.automation_id == value.automation_id,
                BrokerOrderModel.instrument_id == value.instrument_id,
            )
        )
        if matching_order is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="trade_execution",
            )
        require_scoped_reference(
            self._session,
            scope_column=BrokerOrderModel.user_broker_id,
            id_column=BrokerOrderModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.broker_order_id,
            entity_type="trade_execution",
        )
        model = TradeExecutionModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=TradeExecutionModel.fact_id == value.fact_id,
            to_value=_execution_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="trade_execution",
        )

    def replace_order_aggregate(self, user_broker_id: str, value: BrokerOrderDraft) -> BrokerOrderDraft:
        _require_scope(user_broker_id, value.user_broker_id, "broker_order")
        current = self.get_order(user_broker_id, value.id)
        if not self._same_order_intent(current, value):
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="broker_order")
        external_order_id = _external_order_id(value.external_order_id)
        if current.external_order_id is not None:
            if external_order_id is not None and external_order_id != current.external_order_id:
                raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="broker_order")
            external_order_id = current.external_order_id
        with translate_database_errors(entity_type="broker_order"):
            result = self._session.execute(
                update(BrokerOrderModel)
                .where(
                    BrokerOrderModel.user_broker_id == user_broker_id,
                    BrokerOrderModel.id == value.id,
                    BrokerOrderModel.fact_id == value.fact_id,
                    BrokerOrderModel.automation_id == value.automation_id,
                    BrokerOrderModel.decision_id == value.decision_id,
                    BrokerOrderModel.position_cycle_id == value.position_cycle_id,
                    BrokerOrderModel.instrument_id == value.instrument_id,
                    BrokerOrderModel.idempotency_key == value.idempotency_key,
                )
                .values(
                    external_order_id=external_order_id,
                    state=value.state.value,
                    requested_amount=value.requested_amount,
                    executed_amount=value.executed_amount,
                    estimated_commission=value.estimated_commission,
                    executed_commission=value.executed_commission,
                    dispatch_started_at=value.dispatch_started_at,
                    broker_responded_at=value.broker_responded_at,
                    executed_at=value.executed_at,
                    terminal_at=value.terminal_at,
                    updated_at=value.updated_at,
                )
                .returning(BrokerOrderModel)
                .execution_options(populate_existing=True, synchronize_session=False)
            )
        model = result.scalar_one_or_none()
        if model is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="broker_order")
        flush_or_translate(self._session, entity_type="broker_order")
        return _order_value(model)

    @staticmethod
    def _same_order_intent(current: BrokerOrderDraft, candidate: BrokerOrderDraft) -> bool:
        mutable_fields = {
            "external_order_id",
            "state",
            "requested_amount",
            "executed_amount",
            "estimated_commission",
            "executed_commission",
            "dispatch_started_at",
            "broker_responded_at",
            "executed_at",
            "terminal_at",
            "updated_at",
        }
        return current.model_dump(exclude=mutable_fields) == candidate.model_dump(exclude=mutable_fields)

    def _require_common_lineage(
        self,
        user_broker_id: str,
        *,
        automation_id: str,
        cycle_id: str | None,
        instrument_id: str,
        entity_type: str,
    ) -> None:
        automation_instrument = self._session.scalar(
            select(TradingAutomationModel.instrument_id).where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
            )
        )
        if automation_instrument is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)
        cycle = None
        if cycle_id is not None:
            cycle = self._session.execute(
                select(PositionCycleModel.automation_id, PositionCycleModel.instrument_id).where(
                    PositionCycleModel.user_broker_id == user_broker_id,
                    PositionCycleModel.id == cycle_id,
                )
            ).one_or_none()
            if cycle is None:
                raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)
        require_scoped_reference(
            self._session,
            scope_column=BrokerInstrumentModel.user_broker_id,
            id_column=BrokerInstrumentModel.id,
            user_broker_id=user_broker_id,
            reference_id=instrument_id,
            entity_type=entity_type,
        )
        if automation_instrument != instrument_id:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type=entity_type)
        if cycle is not None and (cycle.automation_id != automation_id or cycle.instrument_id != instrument_id):
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type=entity_type)

    def _require_decision_lineage(self, user_broker_id: str, value: BrokerOrderDraft) -> None:
        statement = select(TradeDecisionModel.id).where(
            TradeDecisionModel.user_broker_id == user_broker_id,
            TradeDecisionModel.id == value.decision_id,
            TradeDecisionModel.automation_id == value.automation_id,
            TradeDecisionModel.instrument_id == value.instrument_id,
        )
        if value.position_cycle_id is None:
            statement = statement.where(TradeDecisionModel.position_cycle_id.is_(None))
        else:
            statement = statement.where(TradeDecisionModel.position_cycle_id == value.position_cycle_id)
        if self._session.scalar(statement) is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="broker_order",
            )

    def get_decision(self, user_broker_id: str, decision_id: str) -> TradeDecisionDraft:
        model = self._session.scalar(
            select(TradeDecisionModel).where(
                TradeDecisionModel.user_broker_id == user_broker_id,
                TradeDecisionModel.id == decision_id,
            )
        )
        if model is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="trade_decision")
        return _decision_value(model)

    def get_order(self, user_broker_id: str, order_id: str) -> BrokerOrderDraft:
        model = self._session.scalar(
            select(BrokerOrderModel).where(
                BrokerOrderModel.user_broker_id == user_broker_id,
                BrokerOrderModel.id == order_id,
            )
        )
        if model is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="broker_order")
        return _order_value(model)

    def execution_diagnostics(
        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
    ):
        """Read a bounded scalar projection; deliberately does not acquire row locks."""
        orders = self._session.execute(
            select(
                BrokerOrderModel.idempotency_key,
                BrokerOrderModel.fact_id,
                BrokerOrderModel.id,
                BrokerOrderModel.decision_id,
                TradeDecisionModel.process_id,
                TradeDecisionModel.decision,
                TradeDecisionModel.reason_code,
                TradeDecisionModel.lot_size,
                BrokerOrderModel.external_order_id,
                BrokerOrderModel.state,
                BrokerOrderModel.side,
                BrokerOrderModel.quantity_lots,
                BrokerOrderModel.limit_price,
                BrokerOrderModel.requested_amount,
                BrokerOrderModel.executed_amount,
                BrokerOrderModel.estimated_commission,
                BrokerOrderModel.executed_commission,
                BrokerOrderModel.created_at,
                BrokerOrderModel.dispatch_started_at,
                BrokerOrderModel.broker_responded_at,
                BrokerOrderModel.executed_at,
                BrokerOrderModel.terminal_at,
            )
            .where(
                BrokerOrderModel.user_broker_id == user_broker_id,
                BrokerOrderModel.automation_id == automation_id,
                *(() if intent_id is None else (BrokerOrderModel.idempotency_key == intent_id,)),
            )
            .outerjoin(
                TradeDecisionModel,
                (TradeDecisionModel.user_broker_id == BrokerOrderModel.user_broker_id)
                & (TradeDecisionModel.id == BrokerOrderModel.decision_id),
            )
            .order_by(BrokerOrderModel.created_at.desc(), BrokerOrderModel.id)
            .limit(limit + 1)
        ).all()
        truncated = len(orders) > limit
        orders = orders[:limit]
        order_ids = [row.id for row in orders]
        facts = [row.fact_id for row in orders]
        events = []
        executions = []
        envelopes = []
        if order_ids:
            events = self._session.execute(
                select(
                    BrokerOrderEventModel.id,
                    BrokerOrderEventModel.fact_id,
                    BrokerOrderEventModel.broker_order_id,
                    BrokerOrderEventModel.to_state,
                    BrokerOrderEventModel.safe_reason,
                    BrokerOrderEventModel.occurred_at,
                )
                .where(
                    BrokerOrderEventModel.user_broker_id == user_broker_id,
                    BrokerOrderEventModel.automation_id == automation_id,
                    BrokerOrderEventModel.broker_order_id.in_(order_ids),
                )
                .order_by(BrokerOrderEventModel.occurred_at.desc(), BrokerOrderEventModel.id)
                .limit(limit + 1)
            ).all()
            executions = self._session.execute(
                select(
                    TradeExecutionModel.id,
                    TradeExecutionModel.fact_id,
                    TradeExecutionModel.broker_order_id,
                    TradeExecutionModel.external_execution_id,
                    TradeExecutionModel.side,
                    TradeExecutionModel.executed_lots,
                    TradeExecutionModel.price,
                    TradeExecutionModel.value,
                    TradeExecutionModel.broker_commission,
                    TradeExecutionModel.other_fees,
                    TradeExecutionModel.currency,
                    TradeExecutionModel.executed_at,
                )
                .where(
                    TradeExecutionModel.user_broker_id == user_broker_id,
                    TradeExecutionModel.automation_id == automation_id,
                    TradeExecutionModel.broker_order_id.in_(order_ids),
                )
                .order_by(TradeExecutionModel.executed_at.desc(), TradeExecutionModel.id)
                .limit(limit + 1)
            ).all()
            facts.extend(row.fact_id for row in events[:limit])
            facts.extend(row.fact_id for row in executions[:limit])
            envelopes = self._session.execute(
                select(
                    AutomationEventModel.event_id,
                    AutomationEventModel.sequence_number,
                    AutomationEventModel.fact_kind,
                    AutomationEventModel.received_at,
                )
                .where(
                    AutomationEventModel.user_broker_id == user_broker_id,
                    AutomationEventModel.automation_id == automation_id,
                    AutomationEventModel.event_id.in_(facts),
                )
                .order_by(AutomationEventModel.sequence_number)
                .limit(3 * limit)
            ).all()
        return {
            "truncated": truncated or len(events) > limit or len(executions) > limit,
            "orders": tuple(
                {
                    "intent_id": row.idempotency_key,
                    "fact_id": row.fact_id,
                    "order_id": row.id,
                    "decision_id": row.decision_id,
                    "process_id": row.process_id,
                    "decision": row.decision,
                    "reason_code": row.reason_code,
                    "lot_size": row.lot_size,
                    "external_order_id": row.external_order_id,
                    "state": row.state,
                    "side": row.side,
                    "quantity_lots": row.quantity_lots,
                    "limit_price": row.limit_price,
                    "requested_amount": row.requested_amount,
                    "executed_amount": row.executed_amount,
                    "estimated_commission": row.estimated_commission,
                    "executed_commission": row.executed_commission,
                    "created_at": row.created_at,
                    "dispatch_started_at": row.dispatch_started_at,
                    "broker_responded_at": row.broker_responded_at,
                    "executed_at": row.executed_at,
                    "terminal_at": row.terminal_at,
                }
                for row in orders
            ),
            "events": tuple(
                {
                    "event_id": row.id,
                    "fact_id": row.fact_id,
                    "order_id": row.broker_order_id,
                    "to_state": row.to_state,
                    "safe_reason": row.safe_reason,
                    "occurred_at": row.occurred_at,
                }
                for row in events[:limit]
            ),
            "executions": tuple(
                {
                    "execution_id": row.id,
                    "fact_id": row.fact_id,
                    "order_id": row.broker_order_id,
                    "external_execution_id": row.external_execution_id,
                    "side": row.side,
                    "executed_lots": row.executed_lots,
                    "price": row.price,
                    "value": row.value,
                    "broker_commission": row.broker_commission,
                    "other_fees": row.other_fees,
                    "currency": row.currency,
                    "executed_at": row.executed_at,
                }
                for row in executions[:limit]
            ),
            "accepted_envelopes": tuple(
                {
                    "fact_id": row.event_id,
                    "event_id": row.event_id,
                    "sequence_number": row.sequence_number,
                    "fact_kind": row.fact_kind,
                    "received_at": row.received_at,
                }
                for row in envelopes[: 3 * limit]
            ),
        }

    def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]:
        statement = (
            select(BrokerOrderEventModel)
            .where(
                BrokerOrderEventModel.user_broker_id == user_broker_id,
                BrokerOrderEventModel.broker_order_id == order_id,
            )
            .order_by(BrokerOrderEventModel.occurred_at, BrokerOrderEventModel.id)
        )
        return tuple(_event_value(model) for model in self._session.scalars(statement))

    def list_executions(self, user_broker_id: str, order_id: str) -> tuple[TradeExecutionDraft, ...]:
        statement = (
            select(TradeExecutionModel)
            .where(
                TradeExecutionModel.user_broker_id == user_broker_id,
                TradeExecutionModel.broker_order_id == order_id,
            )
            .order_by(TradeExecutionModel.executed_at, TradeExecutionModel.id)
        )
        return tuple(_execution_value(model) for model in self._session.scalars(statement))
