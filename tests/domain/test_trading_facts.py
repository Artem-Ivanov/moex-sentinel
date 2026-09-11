from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    BrokerAccountFeeProfileDraft,
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
    PositionValuationSnapshotDraft,
    TradeAuditEventDraft,
    TradeAuditLevel,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import AutomationState, DecisionKind

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
MONEY = Decimal("10.25")


def all_fact_drafts() -> tuple[object, ...]:
    automation = TradingAutomationDraft(
        id="automation-1",
        user_broker_id="scope-1",
        instrument_id="instrument-1",
        state=AutomationState.IN_WORK,
        suspended_from_state=None,
        hold_reason=None,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        created_at=NOW,
        updated_at=NOW,
        closed_at=None,
    )
    cycle = PositionCycleDraft(
        id="cycle-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        instrument_id="instrument-1",
        state=PositionCycleState.OPEN,
        quantity_lots=1,
        average_entry_price=MONEY,
        invested_amount=MONEY,
        realized_pnl=Decimal(),
        unrealized_pnl=Decimal(),
        net_pnl=Decimal(),
        accumulated_commissions=Decimal("0.01"),
        opened_at=NOW,
        closed_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    decision = TradeDecisionDraft(
        id="decision-1",
        fact_id="fact-decision-1",
        process_id="process-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        position_cycle_id=cycle.id,
        instrument_id="instrument-1",
        quantity_lots=1,
        lot_size=10,
        average_price=MONEY,
        invested_amount=MONEY,
        current_price=MONEY,
        best_bid=Decimal("10.24"),
        best_ask=Decimal("10.26"),
        indicators={"signal": "synthetic"},
        estimated_commission=Decimal("0.01"),
        decision=DecisionKind.BUY_MORE,
        reason_code="SYNTHETIC_SIGNAL",
        requested_quantity_lots=1,
        limit_price=MONEY,
        strategy_snapshot={"version": 1},
        decided_at=NOW,
        created_at=NOW,
    )
    order = BrokerOrderDraft(
        id="order-1",
        fact_id="fact-order-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        decision_id=decision.id,
        position_cycle_id=cycle.id,
        instrument_id="instrument-1",
        idempotency_key="idempotency-1",
        external_order_id=None,
        intent_kind=OrderIntentKind.BUY_MORE,
        side=OrderSide.BUY,
        order_type=BrokerOrderType.LIMIT,
        state=BrokerOrderStatus.CREATED,
        quantity_lots=1,
        limit_price=MONEY,
        requested_amount=MONEY,
        executed_amount=Decimal(),
        estimated_commission=Decimal("0.01"),
        executed_commission=Decimal(),
        strategy_snapshot={"version": 1},
        created_at=NOW,
        dispatch_started_at=None,
        broker_responded_at=None,
        executed_at=None,
        terminal_at=None,
        updated_at=NOW,
    )
    order_event = BrokerOrderEventDraft(
        id="order-event-1",
        fact_id="fact-order-event-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        broker_order_id=order.id,
        from_state=None,
        to_state=BrokerOrderStatus.CREATED,
        safe_reason="CREATED",
        safe_message="Synthetic order created",
        occurred_at=NOW,
        created_at=NOW,
    )
    execution = TradeExecutionDraft(
        id="execution-1",
        fact_id="fact-execution-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        broker_order_id=order.id,
        position_cycle_id=cycle.id,
        instrument_id="instrument-1",
        external_execution_id="external-execution-1",
        side=OrderSide.BUY,
        executed_lots=1,
        price=MONEY,
        value=MONEY,
        broker_commission=Decimal("0.01"),
        other_fees=Decimal(),
        currency="RUB",
        source=ExecutionSource.BROKER_FILL,
        executed_at=NOW,
        created_at=NOW,
    )
    lot = PositionLotDraft(
        id="lot-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        position_cycle_id=cycle.id,
        buy_execution_id=execution.id,
        source=PositionLotSource.BROKER_EXECUTION,
        original_lots=1,
        remaining_lots=1,
        entry_price=MONEY,
        entry_commission=Decimal("0.01"),
        opened_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    allocation = ExecutionLotAllocationDraft(
        id="allocation-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        position_cycle_id=cycle.id,
        sell_execution_id="execution-sell-1",
        position_lot_id=lot.id,
        allocated_lots=1,
        entry_value=MONEY,
        exit_value=Decimal("11"),
        entry_commission=Decimal("0.01"),
        exit_commission=Decimal("0.01"),
        realized_pnl=Decimal("0.73"),
        allocated_at=NOW,
        created_at=NOW,
    )
    audit = TradeAuditEventDraft(
        event_id="audit-1",
        process_id="process-1",
        parent_process_id=None,
        user_broker_id="scope-1",
        automation_id=automation.id,
        decision_id=decision.id,
        broker_order_id=order.id,
        execution_id=execution.id,
        instrument_id="instrument-1",
        level=TradeAuditLevel.INFO,
        stage="SYNTHETIC_STAGE",
        safe_message="Synthetic audit",
        data={"kind": "synthetic"},
        occurred_at=NOW,
        created_at=NOW,
        critical=False,
    )
    envelope = AutomationEnvelopeDraft(
        event_id="envelope-1",
        automation_id=automation.id,
        user_broker_id="scope-1",
        sequence_number=1,
        expected_revision=1,
        fact_kind="TRADE_DECISION",
        safe_message="Synthetic envelope",
        payload={"fact_id": decision.fact_id},
        occurred_at=NOW,
        received_at=NOW,
    )
    fee_profile = BrokerAccountFeeProfileDraft(
        id="fee-profile-1",
        user_broker_id="scope-1",
        instrument_type="SHARE",
        currency="RUB",
        buy_rate=Decimal("0.0005"),
        sell_rate=Decimal("0.0005"),
        service_rate=Decimal(),
        deal_rate=Decimal(),
        source="SYNTHETIC",
        calculated_at=NOW,
        valid_until=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    valuation = PositionValuationSnapshotDraft(
        id="valuation-1",
        user_broker_id="scope-1",
        automation_id=automation.id,
        position_cycle_id=cycle.id,
        instrument_id="instrument-1",
        quantity_lots=1,
        average_price=MONEY,
        current_price=Decimal("11"),
        invested_amount=MONEY,
        market_value=Decimal("11"),
        realized_pnl=Decimal(),
        unrealized_pnl=Decimal("0.75"),
        net_pnl=Decimal("0.74"),
        actual_commissions=Decimal("0.01"),
        source="SYNTHETIC",
        captured_at=NOW,
        created_at=NOW,
    )
    return (
        automation,
        cycle,
        decision,
        order,
        order_event,
        execution,
        lot,
        allocation,
        audit,
        envelope,
        fee_profile,
        valuation,
    )


@pytest.mark.parametrize("draft", all_fact_drafts())
def test_fact_drafts_are_frozen_and_forbid_extra_fields(draft: object) -> None:
    model_type = cast(type[BaseModel], type(draft))
    model_dump = draft.model_dump()  # type: ignore[attr-defined]

    with pytest.raises(ValidationError):
        model_type.model_validate({**model_dump, "unexpected": True})
    with pytest.raises(ValidationError):
        draft.__setattr__(next(iter(model_dump)), "changed")


def test_invalid_lifecycle_value_is_rejected() -> None:
    cycle = next(value for value in all_fact_drafts() if isinstance(value, PositionCycleDraft))

    with pytest.raises(ValidationError):
        PositionCycleDraft.model_validate({**cycle.model_dump(), "state": "BROKEN"})


def test_persistence_error_exposes_stable_code_without_payload() -> None:
    error = TradingFactPersistenceError(
        TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_execution",
        constraint_name="uq_trade_executions_fact_id",
    )

    assert error.code is TradingFactErrorCode.FACT_ID_CONFLICT
    assert error.entity_type == "trade_execution"
    assert error.constraint_name == "uq_trade_executions_fact_id"
    assert "synthetic-payload" not in str(error)
