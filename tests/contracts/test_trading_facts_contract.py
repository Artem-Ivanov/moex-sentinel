"""Strict shared Worker-to-Core trading-fact contract."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.time import require_utc_millisecond, utc_now_ms
from sentinel_contracts.trading import AutomationState, DecisionKind
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStateChangedEnvelope,
    AutomationStateChangedPayload,
    AutomationStatus,
    AutomationStatusesResult,
    BrokerOrderRecordedEnvelope,
    BrokerOrderRecordedPayload,
    BrokerOrderStateChangedEnvelope,
    BrokerOrderStateChangedPayload,
    BrokerPositionBootstrap,
    ExecutionLotAllocatedEnvelope,
    ExecutionLotAllocatedPayload,
    FactBatchRequest,
    FactBatchResult,
    FactBrokerOrderStatus,
    FactBrokerOrderType,
    FactEnvelope,
    FactExecutionSource,
    FactGroupAcknowledgement,
    FactGroupFailure,
    FactIngressErrorCode,
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


def test_clock_and_validator_enforce_utc_millisecond_precision() -> None:
    generated = utc_now_ms()

    assert generated.utcoffset() == timedelta(0)
    assert generated.microsecond % 1000 == 0
    assert require_utc_millisecond(NOW) == NOW
    with pytest.raises(ValueError, match="UTC"):
        require_utc_millisecond(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="milliseconds"):
        require_utc_millisecond(NOW.replace(microsecond=123001))


def test_fact_union_round_trips_every_kind_with_explicit_utc_offset() -> None:
    request = FactBatchRequest(facts=list(all_envelopes()))
    payload = request.model_dump_json()
    restored = FactBatchRequest.model_validate_json(payload)

    assert {item.fact_kind for item in restored.facts} == set(FactKind)
    assert '"occurred_at":"2026-08-13T10:00:00.123Z"' in payload


def test_fact_union_rejects_discriminator_payload_mismatch() -> None:
    value = all_envelopes()[0].model_dump(mode="python")
    value["fact_kind"] = FactKind.TRADE_DECISION_RECORDED

    with pytest.raises(ValidationError):
        TypeAdapter(FactEnvelope).validate_python(value)


def test_envelope_rejects_extra_fields_bad_uuid_and_sub_millisecond_time() -> None:
    base = all_envelopes()[0].model_dump(mode="python")

    for update in (
        {"unexpected": True},
        {"event_id": "not-a-uuid"},
        {"occurred_at": NOW.replace(microsecond=123001)},
    ):
        with pytest.raises(ValidationError):
            AutomationStateChangedEnvelope.model_validate({**base, **update})


def test_envelope_is_frozen() -> None:
    envelope = all_envelopes()[0]

    with pytest.raises(ValidationError):
        envelope.sequence_number = 2


def test_command_status_and_results_are_strict_safe_values() -> None:
    bootstrap = BrokerPositionBootstrap(
        position_cycle_id=CYCLE_ID,
        position_lot_id=LOT_ID,
        quantity_lots=3,
        average_price=Decimal("101.25"),
        invested_amount=Decimal("3037.50"),
        currency="RUB",
        observed_at=NOW,
    )
    command = AutomationCommand(
        automation_id=AUTOMATION_ID,
        user_broker_id=SCOPE_ID,
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        external_instrument_id="synthetic-instrument",
        instrument_id=INSTRUMENT_ID,
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        state=AutomationState.IN_QUEUE,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        bootstrap=bootstrap,
    )
    statuses = AutomationStatusesResult(
        automations=(
            AutomationStatus(
                automation_id=command.automation_id,
                user_broker_id=command.user_broker_id,
                state=command.state,
                revision=command.revision,
                last_sequence_number=command.last_sequence_number,
                resume_requested=command.resume_requested,
            ),
        )
    )
    result = FactBatchResult(
        results=(
            FactGroupAcknowledgement(
                automation_id=AUTOMATION_ID,
                accepted_through_sequence=9,
                current_revision=2,
                accepted_event_ids=(EVENT_ID,),
            ),
        ),
        failures=(
            FactGroupFailure(
                automation_id=UUID("00000000-0000-4000-8000-000000000031"),
                code=FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
                event_ids=(UUID("00000000-0000-4000-8000-000000000032"),),
                sequence_numbers=(1,),
                retryable=False,
            ),
        ),
    )

    assert command.bootstrap == bootstrap
    assert statuses.automations[0].automation_id == command.automation_id
    assert result.failures[0].retryable is False
    assert "payload" not in result.model_dump_json()


def test_command_rejects_persisted_strategy_fields() -> None:
    values = {
        "automation_id": AUTOMATION_ID,
        "user_broker_id": SCOPE_ID,
        "broker_id": BROKER_ID,
        "account_id": "synthetic-account",
        "external_instrument_id": "synthetic-instrument",
        "instrument_id": INSTRUMENT_ID,
        "currency": "RUB",
        "lot_size": 10,
        "min_price_increment": Decimal("0.01"),
        "state": AutomationState.IN_QUEUE,
        "revision": 1,
        "last_sequence_number": 0,
        "resume_requested": False,
        "bootstrap": None,
        "strategy": {"initial_order_amount": "1000"},
    }

    with pytest.raises(ValidationError):
        AutomationCommand.model_validate(values)


def test_bootstrap_timestamp_serializes_with_millisecond_precision() -> None:
    bootstrap = BrokerPositionBootstrap(
        position_cycle_id=CYCLE_ID,
        position_lot_id=LOT_ID,
        quantity_lots=1,
        average_price=Decimal("100"),
        invested_amount=Decimal("1000"),
        currency="RUB",
        observed_at=NOW,
    )

    assert '"observed_at":"2026-08-13T10:00:00.123Z"' in bootstrap.model_dump_json()

    with pytest.raises(ValidationError):
        BrokerPositionBootstrap.model_validate(
            {**bootstrap.model_dump(mode="python"), "observed_at": NOW.replace(microsecond=123001)}
        )
