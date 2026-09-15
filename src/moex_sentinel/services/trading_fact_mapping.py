"""Explicit mapping from shared transport envelopes to Core fact drafts."""

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    BrokerOrderStatus,
    BrokerOrderType,
    ExecutionLotAllocationDraft,
    ExecutionSource,
    OrderIntentKind,
    PositionCycleDraft,
    PositionCycleState,
    PositionLotDraft,
    PositionLotSource,
    TradeAuditEventDraft,
    TradeAuditLevel,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.services.trading_fact_ports import TradingFactsUnitOfWorkPort
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    BrokerOrderRecordedEnvelope,
    BrokerOrderStateChangedEnvelope,
    ExecutionLotAllocatedEnvelope,
    FactEnvelope,
    PositionCycleUpdatedEnvelope,
    PositionLotOpenedEnvelope,
    TradeAuditRecordedEnvelope,
    TradeDecisionRecordedEnvelope,
    TradeExecutionRecordedEnvelope,
)


class TradingFactMapper:
    """Apply one already validated envelope to session-bound repositories."""

    def apply(self, uow: TradingFactsUnitOfWorkPort, envelope: FactEnvelope) -> None:
        """Map an already validated envelope within the supplied UoW without committing it."""
        scope_id = str(envelope.user_broker_id)
        automation_id = str(envelope.automation_id)
        match envelope:
            case AutomationStateChangedEnvelope():
                return
            case TradeDecisionRecordedEnvelope(payload=payload):
                uow.orders.append_decision(
                    scope_id,
                    TradeDecisionDraft(
                        id=str(payload.decision_id),
                        fact_id=str(envelope.event_id),
                        process_id=str(payload.process_id) if payload.process_id else None,
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        position_cycle_id=str(payload.position_cycle_id) if payload.position_cycle_id else None,
                        instrument_id=str(payload.instrument_id),
                        quantity_lots=payload.quantity_lots,
                        lot_size=payload.lot_size,
                        average_price=payload.average_price,
                        invested_amount=payload.invested_amount,
                        current_price=payload.current_price,
                        best_bid=payload.best_bid,
                        best_ask=payload.best_ask,
                        indicators=payload.indicators,
                        estimated_commission=payload.estimated_commission,
                        decision=payload.decision,
                        reason_code=payload.reason_code,
                        requested_quantity_lots=payload.requested_quantity_lots,
                        limit_price=payload.limit_price,
                        strategy_snapshot=payload.strategy_snapshot,
                        decided_at=payload.decided_at,
                        created_at=payload.created_at,
                    ),
                )
            case BrokerOrderRecordedEnvelope():
                uow.orders.append_order(scope_id, self._new_order(envelope))
            case BrokerOrderStateChangedEnvelope():
                self._apply_order_state_change(uow, envelope)
            case TradeExecutionRecordedEnvelope(payload=payload):
                uow.orders.append_execution(
                    scope_id,
                    TradeExecutionDraft(
                        id=str(payload.execution_id),
                        fact_id=str(envelope.event_id),
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        broker_order_id=str(payload.broker_order_id),
                        position_cycle_id=str(payload.position_cycle_id),
                        instrument_id=str(payload.instrument_id),
                        external_execution_id=payload.external_execution_id,
                        side=payload.side,
                        executed_lots=payload.executed_lots,
                        price=payload.price,
                        value=payload.value,
                        broker_commission=payload.broker_commission,
                        other_fees=payload.other_fees,
                        currency=payload.currency,
                        source=ExecutionSource(payload.source.value),
                        executed_at=payload.executed_at,
                        created_at=payload.created_at,
                    ),
                )
            case PositionCycleUpdatedEnvelope():
                self._apply_cycle_update(uow, envelope)
            case PositionLotOpenedEnvelope(payload=payload):
                uow.positions.append_lot(
                    scope_id,
                    PositionLotDraft(
                        id=str(payload.position_lot_id),
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        position_cycle_id=str(payload.position_cycle_id),
                        buy_execution_id=(str(payload.buy_execution_id) if payload.buy_execution_id else None),
                        source=PositionLotSource(payload.source.value),
                        original_lots=payload.original_lots,
                        remaining_lots=payload.remaining_lots,
                        entry_price=payload.entry_price,
                        entry_commission=payload.entry_commission,
                        opened_at=payload.opened_at,
                        created_at=payload.created_at,
                        updated_at=payload.updated_at,
                    ),
                )
            case ExecutionLotAllocatedEnvelope(payload=payload):
                uow.positions.append_allocation_and_decrement(
                    scope_id,
                    ExecutionLotAllocationDraft(
                        id=str(payload.allocation_id),
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        position_cycle_id=str(payload.position_cycle_id),
                        sell_execution_id=str(payload.sell_execution_id),
                        position_lot_id=str(payload.position_lot_id),
                        allocated_lots=payload.allocated_lots,
                        entry_value=payload.entry_value,
                        exit_value=payload.exit_value,
                        entry_commission=payload.entry_commission,
                        exit_commission=payload.exit_commission,
                        realized_pnl=payload.realized_pnl,
                        allocated_at=payload.allocated_at,
                        created_at=payload.created_at,
                    ),
                    expected_remaining_lots=payload.remaining_lots_after + payload.allocated_lots,
                    remaining_lots_after=payload.remaining_lots_after,
                )
            case TradeAuditRecordedEnvelope(payload=payload):
                uow.audit.append_audit(
                    scope_id,
                    TradeAuditEventDraft(
                        event_id=str(payload.audit_event_id),
                        process_id=str(payload.process_id),
                        parent_process_id=str(payload.parent_process_id) if payload.parent_process_id else None,
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        decision_id=str(payload.decision_id) if payload.decision_id else None,
                        broker_order_id=str(payload.broker_order_id) if payload.broker_order_id else None,
                        execution_id=str(payload.execution_id) if payload.execution_id else None,
                        instrument_id=str(payload.instrument_id),
                        level=TradeAuditLevel(payload.level.value),
                        stage=payload.stage,
                        safe_message=payload.safe_message,
                        data=payload.data,
                        occurred_at=payload.occurred_at,
                        created_at=payload.created_at,
                        critical=payload.critical,
                    ),
                )
            case _:
                raise AssertionError(f"Unsupported fact envelope type: {type(envelope).__name__}")

    @staticmethod
    def _apply_order_state_change(uow: TradingFactsUnitOfWorkPort, envelope: BrokerOrderStateChangedEnvelope) -> None:
        """Validate the transition and update its aggregate without duplicating terminal repairs."""
        payload = envelope.payload
        scope_id = str(envelope.user_broker_id)
        automation_id = str(envelope.automation_id)
        if payload.order_id != payload.broker_order_id or payload.state != payload.to_state:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="broker_order_event",
            )
        current = uow.orders.get_order(scope_id, str(payload.broker_order_id))
        from_state = BrokerOrderStatus(payload.from_state.value) if payload.from_state else None
        terminal_states = {
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELLED,
            BrokerOrderStatus.REJECTED,
            BrokerOrderStatus.EXPIRED,
            BrokerOrderStatus.FAILED,
        }
        to_state = BrokerOrderStatus(payload.to_state.value)
        terminal_repair = (
            from_state is BrokerOrderStatus.UNCERTAIN and current.state in terminal_states and to_state is current.state
        )
        if (from_state is not current.state and not terminal_repair) or (
            current.state in terminal_states and to_state is not current.state
        ):
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="broker_order_event",
            )
        replacement = BrokerOrderDraft(
            id=current.id,
            fact_id=current.fact_id,
            user_broker_id=scope_id,
            automation_id=automation_id,
            decision_id=str(payload.decision_id),
            position_cycle_id=str(payload.position_cycle_id) if payload.position_cycle_id else None,
            instrument_id=str(payload.instrument_id),
            idempotency_key=payload.idempotency_key,
            external_order_id=payload.external_order_id,
            intent_kind=OrderIntentKind(payload.intent_kind.value),
            side=payload.side,
            order_type=BrokerOrderType(payload.order_type.value),
            state=BrokerOrderStatus(payload.state.value),
            quantity_lots=payload.quantity_lots,
            limit_price=payload.limit_price,
            requested_amount=payload.requested_amount,
            executed_amount=payload.executed_amount,
            estimated_commission=payload.estimated_commission,
            executed_commission=payload.executed_commission,
            strategy_snapshot=payload.strategy_snapshot,
            created_at=current.created_at,
            dispatch_started_at=payload.dispatch_started_at,
            broker_responded_at=payload.broker_responded_at,
            executed_at=payload.executed_at,
            terminal_at=payload.terminal_at,
            updated_at=payload.updated_at,
        )
        uow.orders.replace_order_aggregate(scope_id, replacement)
        if not terminal_repair:
            uow.orders.append_order_event(
                scope_id,
                BrokerOrderEventDraft(
                    id=str(payload.order_event_id),
                    fact_id=str(envelope.event_id),
                    user_broker_id=scope_id,
                    automation_id=automation_id,
                    broker_order_id=str(payload.broker_order_id),
                    from_state=BrokerOrderStatus(payload.from_state.value) if payload.from_state else None,
                    to_state=BrokerOrderStatus(payload.to_state.value),
                    safe_reason=payload.safe_reason,
                    safe_message=payload.safe_message,
                    occurred_at=payload.occurred_at,
                    created_at=payload.created_at,
                ),
            )

    @staticmethod
    def _apply_cycle_update(uow: TradingFactsUnitOfWorkPort, envelope: PositionCycleUpdatedEnvelope) -> None:
        """Open a missing cycle or replace its aggregate within the current automation group."""
        payload = envelope.payload
        scope_id = str(envelope.user_broker_id)
        automation_id = str(envelope.automation_id)
        cycle = PositionCycleDraft(
            id=str(payload.position_cycle_id),
            user_broker_id=scope_id,
            automation_id=automation_id,
            instrument_id=str(payload.instrument_id),
            state=PositionCycleState(payload.state.value),
            quantity_lots=payload.quantity_lots,
            average_entry_price=payload.average_entry_price,
            invested_amount=payload.invested_amount,
            realized_pnl=payload.realized_pnl,
            unrealized_pnl=payload.unrealized_pnl,
            net_pnl=payload.net_pnl,
            accumulated_commissions=payload.accumulated_commissions,
            opened_at=payload.opened_at,
            closed_at=payload.closed_at,
            created_at=payload.created_at,
            updated_at=payload.updated_at,
        )
        try:
            uow.positions.get_cycle(scope_id, cycle.id)
        except TradingFactPersistenceError as error:
            if error.code is not TradingFactErrorCode.NOT_FOUND:
                raise
            uow.positions.open_cycle(scope_id, cycle)
        else:
            uow.positions.replace_cycle_aggregate(scope_id, cycle)

    @staticmethod
    def _new_order(envelope: BrokerOrderRecordedEnvelope) -> BrokerOrderDraft:
        payload = envelope.payload
        return BrokerOrderDraft(
            id=str(payload.order_id),
            fact_id=str(envelope.event_id),
            user_broker_id=str(envelope.user_broker_id),
            automation_id=str(envelope.automation_id),
            decision_id=str(payload.decision_id),
            position_cycle_id=str(payload.position_cycle_id) if payload.position_cycle_id else None,
            instrument_id=str(payload.instrument_id),
            idempotency_key=payload.idempotency_key,
            external_order_id=payload.external_order_id,
            intent_kind=OrderIntentKind(payload.intent_kind.value),
            side=payload.side,
            order_type=BrokerOrderType(payload.order_type.value),
            state=BrokerOrderStatus(payload.state.value),
            quantity_lots=payload.quantity_lots,
            limit_price=payload.limit_price,
            requested_amount=payload.requested_amount,
            executed_amount=payload.executed_amount,
            estimated_commission=payload.estimated_commission,
            executed_commission=payload.executed_commission,
            strategy_snapshot=payload.strategy_snapshot,
            created_at=payload.created_at,
            dispatch_started_at=payload.dispatch_started_at,
            broker_responded_at=payload.broker_responded_at,
            executed_at=payload.executed_at,
            terminal_at=payload.terminal_at,
            updated_at=payload.updated_at,
        )
