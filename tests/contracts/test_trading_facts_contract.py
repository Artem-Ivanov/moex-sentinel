"""Strict shared Worker-to-Core trading-fact contract."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from sentinel_contracts.time import require_utc_millisecond, utc_now_ms
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStateChangedEnvelope,
    AutomationStatus,
    AutomationStatusesResult,
    BrokerPositionBootstrap,
    FactBatchRequest,
    FactBatchResult,
    FactEnvelope,
    FactGroupAcknowledgement,
    FactGroupFailure,
    FactIngressErrorCode,
    FactKind,
)
from tests.contracts.trading_facts_helpers import (
    AUTOMATION_ID,
    BROKER_ID,
    CYCLE_ID,
    EVENT_ID,
    INSTRUMENT_ID,
    LOT_ID,
    NOW,
    SCOPE_ID,
    all_envelopes,
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
