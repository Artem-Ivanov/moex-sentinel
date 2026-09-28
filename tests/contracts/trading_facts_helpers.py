"""Synthetic trading-fact envelopes shared across boundary tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import AutomationState, DecisionKind
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    AutomationStateChangedPayload,
    BrokerOrderRecordedEnvelope,
    BrokerOrderRecordedPayload,
    BrokerOrderStateChangedEnvelope,
    BrokerOrderStateChangedPayload,
    ExecutionLotAllocatedEnvelope,
    ExecutionLotAllocatedPayload,
    FactBrokerOrderStatus,
    FactBrokerOrderType,
    FactEnvelope,
    FactExecutionSource,
    FactKind,
    FactOrderIntentKind,
    FactPositionCycleState,
    FactTradeAuditLevel,
    PositionCycleUpdatedEnvelope,
    PositionCycleUpdatedPayload,
    PositionLotOpenedEnvelope,
    PositionLotOpenedPayload,
    PositionLotSource,
    TradeAuditRecordedEnvelope,
    TradeAuditRecordedPayload,
    TradeDecisionRecordedEnvelope,
    TradeDecisionRecordedPayload,
    TradeExecutionRecordedEnvelope,
    TradeExecutionRecordedPayload,
)

NOW = datetime(2026, 8, 13, 10, 0, 0, 123000, tzinfo=UTC)
EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")
SCOPE_ID = UUID("00000000-0000-4000-8000-000000000002")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000004")
CYCLE_ID = UUID("00000000-0000-4000-8000-000000000005")
DECISION_ID = UUID("00000000-0000-4000-8000-000000000006")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000007")
EXECUTION_ID = UUID("00000000-0000-4000-8000-000000000008")
LOT_ID = UUID("00000000-0000-4000-8000-000000000009")
BROKER_ID = UUID("00000000-0000-4000-8000-000000000030")


def envelope_values() -> dict[str, object]:
    return {
        "event_id": EVENT_ID,
        "user_broker_id": SCOPE_ID,
        "automation_id": AUTOMATION_ID,
        "sequence_number": 1,
        "expected_revision": 1,
        "safe_message": "Synthetic fact",
        "occurred_at": NOW,
    }


def order_payload_values() -> dict[str, object]:
    return {
        "order_id": ORDER_ID,
        "decision_id": DECISION_ID,
        "position_cycle_id": CYCLE_ID,
        "instrument_id": INSTRUMENT_ID,
        "idempotency_key": "synthetic-order-key",
        "external_order_id": None,
        "intent_kind": FactOrderIntentKind.OPEN,
        "side": OrderSide.BUY,
        "order_type": FactBrokerOrderType.LIMIT,
        "state": FactBrokerOrderStatus.DISPATCH_PENDING,
        "quantity_lots": 1,
        "limit_price": Decimal("100"),
        "requested_amount": Decimal("1000"),
        "executed_amount": Decimal("0"),
        "estimated_commission": Decimal("1"),
        "executed_commission": Decimal("0"),
        "strategy_snapshot": {"currency": "RUB"},
        "created_at": NOW,
        "dispatch_started_at": None,
        "broker_responded_at": None,
        "executed_at": None,
        "terminal_at": None,
        "updated_at": NOW,
    }


def all_envelopes() -> tuple[FactEnvelope, ...]:
    common = envelope_values()
    order = order_payload_values()
    return (
        AutomationStateChangedEnvelope.model_validate(
            {
                **common,
                "fact_kind": FactKind.AUTOMATION_STATE_CHANGED,
                "payload": AutomationStateChangedPayload(
                    state=AutomationState.IN_WORK,
                    suspended_from_state=None,
                    hold_reason=None,
                    closed_at=None,
                ),
            }
        ),
        TradeDecisionRecordedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000011"),
                "sequence_number": 2,
                "fact_kind": FactKind.TRADE_DECISION_RECORDED,
                "payload": TradeDecisionRecordedPayload(
                    decision_id=DECISION_ID,
                    process_id=None,
                    position_cycle_id=CYCLE_ID,
                    instrument_id=INSTRUMENT_ID,
                    quantity_lots=0,
                    lot_size=10,
                    average_price=Decimal("0"),
                    invested_amount=Decimal("0"),
                    current_price=Decimal("100"),
                    best_bid=Decimal("99.9"),
                    best_ask=Decimal("100"),
                    indicators={"signal": "synthetic"},
                    estimated_commission=Decimal("1"),
                    decision=DecisionKind.BUY_MORE,
                    reason_code="ENTRY_SIGNAL",
                    requested_quantity_lots=1,
                    limit_price=Decimal("100"),
                    strategy_snapshot={"currency": "RUB"},
                    decided_at=NOW,
                    created_at=NOW,
                ),
            }
        ),
        BrokerOrderRecordedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000012"),
                "sequence_number": 3,
                "fact_kind": FactKind.BROKER_ORDER_RECORDED,
                "payload": BrokerOrderRecordedPayload.model_validate(order),
            }
        ),
        BrokerOrderStateChangedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000013"),
                "sequence_number": 4,
                "fact_kind": FactKind.BROKER_ORDER_STATE_CHANGED,
                "payload": BrokerOrderStateChangedPayload.model_validate(
                    {
                        **{key: value for key, value in order.items() if key != "created_at"},
                        "order_event_id": UUID("00000000-0000-4000-8000-000000000014"),
                        "broker_order_id": ORDER_ID,
                        "from_state": FactBrokerOrderStatus.DISPATCH_PENDING,
                        "to_state": FactBrokerOrderStatus.SUBMITTING,
                        "safe_reason": "SUBMITTING",
                        "safe_message": "Synthetic order transition",
                        "occurred_at": NOW,
                        "created_at": NOW,
                    }
                ),
            }
        ),
        PositionCycleUpdatedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000015"),
                "sequence_number": 5,
                "fact_kind": FactKind.POSITION_CYCLE_UPDATED,
                "payload": PositionCycleUpdatedPayload(
                    position_cycle_id=CYCLE_ID,
                    instrument_id=INSTRUMENT_ID,
                    state=FactPositionCycleState.OPEN,
                    quantity_lots=1,
                    average_entry_price=Decimal("100"),
                    invested_amount=Decimal("1000"),
                    realized_pnl=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                    net_pnl=Decimal("0"),
                    accumulated_commissions=Decimal("1"),
                    opened_at=NOW,
                    closed_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            }
        ),
        TradeExecutionRecordedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000016"),
                "sequence_number": 6,
                "fact_kind": FactKind.TRADE_EXECUTION_RECORDED,
                "payload": TradeExecutionRecordedPayload(
                    execution_id=EXECUTION_ID,
                    broker_order_id=ORDER_ID,
                    position_cycle_id=CYCLE_ID,
                    instrument_id=INSTRUMENT_ID,
                    external_execution_id="synthetic-execution",
                    side=OrderSide.BUY,
                    executed_lots=1,
                    price=Decimal("100"),
                    value=Decimal("1000"),
                    broker_commission=Decimal("1"),
                    other_fees=Decimal("0"),
                    currency="RUB",
                    source=FactExecutionSource.BROKER_FILL,
                    executed_at=NOW,
                    created_at=NOW,
                ),
            }
        ),
        PositionLotOpenedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000017"),
                "sequence_number": 7,
                "fact_kind": FactKind.POSITION_LOT_OPENED,
                "payload": PositionLotOpenedPayload(
                    position_lot_id=LOT_ID,
                    position_cycle_id=CYCLE_ID,
                    buy_execution_id=EXECUTION_ID,
                    source=PositionLotSource.BROKER_EXECUTION,
                    original_lots=1,
                    remaining_lots=1,
                    entry_price=Decimal("100"),
                    entry_commission=Decimal("1"),
                    opened_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            }
        ),
        ExecutionLotAllocatedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000018"),
                "sequence_number": 8,
                "fact_kind": FactKind.EXECUTION_LOT_ALLOCATED,
                "payload": ExecutionLotAllocatedPayload(
                    allocation_id=UUID("00000000-0000-4000-8000-000000000019"),
                    position_cycle_id=CYCLE_ID,
                    sell_execution_id=EXECUTION_ID,
                    position_lot_id=LOT_ID,
                    allocated_lots=1,
                    remaining_lots_after=0,
                    entry_value=Decimal("1000"),
                    exit_value=Decimal("1100"),
                    entry_commission=Decimal("1"),
                    exit_commission=Decimal("1"),
                    realized_pnl=Decimal("98"),
                    allocated_at=NOW,
                    created_at=NOW,
                ),
            }
        ),
        TradeAuditRecordedEnvelope.model_validate(
            {
                **common,
                "event_id": UUID("00000000-0000-4000-8000-000000000020"),
                "sequence_number": 9,
                "fact_kind": FactKind.TRADE_AUDIT_RECORDED,
                "payload": TradeAuditRecordedPayload(
                    audit_event_id=UUID("00000000-0000-4000-8000-000000000021"),
                    process_id=UUID("00000000-0000-4000-8000-000000000022"),
                    parent_process_id=None,
                    decision_id=DECISION_ID,
                    broker_order_id=ORDER_ID,
                    execution_id=EXECUTION_ID,
                    instrument_id=INSTRUMENT_ID,
                    level=FactTradeAuditLevel.INFO,
                    stage="SYNTHETIC_STAGE",
                    safe_message="Synthetic audit",
                    data={"result": "ok"},
                    occurred_at=NOW,
                    created_at=NOW,
                    critical=False,
                ),
            }
        ),
    )
