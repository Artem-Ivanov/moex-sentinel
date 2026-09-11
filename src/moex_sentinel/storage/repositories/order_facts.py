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
from moex_sentinel.storage.repositories.trading_facts_support import (
    append_idempotent,
    flush_or_translate,
    require_scoped_reference,
)
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import DecisionKind


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
        external_order_id=model.external_order_id,
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
        model = BrokerOrderModel(**value.model_dump(mode="python"))
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
                external_order_id=value.external_order_id,
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
            .returning(BrokerOrderModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type="broker_order")
        flush_or_translate(self._session, entity_type="broker_order")
        return self.get_order(user_broker_id, value.id)

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
        for scope_column, id_column, reference_id in (
            (TradingAutomationModel.user_broker_id, TradingAutomationModel.id, automation_id),
            (PositionCycleModel.user_broker_id, PositionCycleModel.id, cycle_id),
            (BrokerInstrumentModel.user_broker_id, BrokerInstrumentModel.id, instrument_id),
        ):
            require_scoped_reference(
                self._session,
                scope_column=scope_column,
                id_column=id_column,
                user_broker_id=user_broker_id,
                reference_id=reference_id,
                entity_type=entity_type,
            )
        matching_automation = self._session.scalar(
            select(TradingAutomationModel.id).where(
                TradingAutomationModel.user_broker_id == user_broker_id,
                TradingAutomationModel.id == automation_id,
                TradingAutomationModel.instrument_id == instrument_id,
            )
        )
        if matching_automation is None:
            raise TradingFactPersistenceError(TradingFactErrorCode.INVALID_STATE, entity_type=entity_type)
        if cycle_id is None:
            return
        matching_cycle = self._session.scalar(
            select(PositionCycleModel.id).where(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == cycle_id,
                PositionCycleModel.automation_id == automation_id,
                PositionCycleModel.instrument_id == instrument_id,
            )
        )
        if matching_cycle is None:
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
