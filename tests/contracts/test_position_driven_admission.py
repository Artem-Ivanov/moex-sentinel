import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from sentinel_contracts import trading_admission as admission
from sentinel_contracts.broker_execution import BrokerOrderState

NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)
ID = UUID("00000000-0000-4000-8000-000000000001")
FINGERPRINT = "a" * 64


def limits_values():
    return {
        "currency": "RUB",
        "max_order_notional": "5001.1",
        "max_exposure": "5052.3",
        "max_outstanding_orders": 1,
        "max_daily_loss": "252.6040",
    }


def reconciliation_values():
    return {
        "reconciliation_id": ID,
        "broker_id": ID,
        "account_id": "account-1",
        "scope": "ACCOUNT",
        "owner_runtime_instance_id": ID,
        "owner_generation": 5,
        "configuration_fingerprint": FINGERPRINT,
        "status": "ACCOUNT_ONLY",
        "captured_at": NOW,
        "valid_until": NOW + timedelta(seconds=1),
        "can_arm": True,
        "position_anchor_units": None,
        "active_ordinary_order_count": 0,
        "active_stop_order_count": 0,
        "unresolved_reservation_count": 0,
        "reasons": (),
        "risk_snapshot_id": ID,
    }


def permit_values():
    return {
        "permit_id": ID,
        "runtime_instance_id": ID,
        "idempotency_key": ID,
        "generation": 5,
        "revision": 12,
        "configuration_fingerprint": FINGERPRINT,
        "request_fingerprint": FINGERPRINT,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(milliseconds=500),
        "server_response_at": NOW + timedelta(milliseconds=100),
        "risk_snapshot_id": ID,
    }


def state_values():
    return {
        "broker_order_id": "",
        "idempotency_key": str(ID),
        "status": "CANCELLED",
        "requested_lots": 1,
        "executed_lots": 0,
        "requested_amount": Decimal("100"),
        "executed_amount": Decimal(0),
        "estimated_commission": Decimal(1),
        "executed_commission": Decimal(0),
        "currency": "RUB",
    }


def test_canonical_limits_and_legacy_json_preserve_all_four_caps():
    canonical = admission.AdmissionLimits(**limits_values())
    legacy_values = limits_values() | {"canary_uid": "old-uid", "exposure_scope": "CANARY_UID_GROSS"}
    legacy = admission.AdmissionLimits.model_validate_json(json.dumps(legacy_values))
    assert canonical == legacy
    assert admission.AdmissionLimits.model_validate(legacy_values) == canonical
    assert canonical.exposure_scope == "ACCOUNT_GROSS"
    encoded = legacy.model_dump(mode="json")
    assert "canary_uid" not in encoded
    assert encoded == limits_values() | {"exposure_scope": "ACCOUNT_GROSS"}
    assert admission.AdmissionLimits.model_validate_json(legacy.model_dump_json()) == canonical
    assert legacy_values["canary_uid"] == "old-uid"
    assert legacy_values["exposure_scope"] == "CANARY_UID_GROSS"


@pytest.mark.parametrize(
    "changes",
    [
        {"exposure_scope": "CANARY_UID_GROSS"},
        {"canary_uid": "uid"},
        {"canary_uid": "uid", "exposure_scope": "ACCOUNT_GROSS"},
        {"canary_uid": "", "exposure_scope": "CANARY_UID_GROSS"},
        {"canary_uid": None, "exposure_scope": "CANARY_UID_GROSS"},
        {"canary_uid": 3, "exposure_scope": "CANARY_UID_GROSS"},
        {"canary_uid": "u" * 129, "exposure_scope": "CANARY_UID_GROSS"},
        {"exposure_scope": "UNKNOWN"},
        {"unexpected": "value"},
        {"max_order_notional": 1.1},
        {"max_order_notional": True},
        {"max_exposure": "NaN"},
        {"max_exposure": "-1"},
        {"max_daily_loss": "Infinity"},
        {"max_outstanding_orders": True},
        {"max_outstanding_orders": "1"},
        {"max_daily_loss": "0"},
        {"currency": "USD"},
        {"max_daily_loss": "1.0000000001"},
        {"max_order_notional": "10000000000000000000"},
    ],
)
def test_limits_reject_invalid_legacy_pairs_and_unsafe_caps(changes):
    with pytest.raises(ValidationError):
        admission.AdmissionLimits(**(limits_values() | changes))


def test_update_limits_requires_a_strict_revision_and_validated_limits():
    request = admission.UpdateLimitsRequest(expected_revision=12, limits=limits_values())
    assert request.limits.exposure_scope == "ACCOUNT_GROSS"
    assert request.expected_revision == 12
    for revision in (0, True, "12", 1.5):
        with pytest.raises(ValidationError):
            admission.UpdateLimitsRequest(expected_revision=revision, limits=limits_values())
    with pytest.raises(ValidationError):
        admission.UpdateLimitsRequest(expected_revision=12, limits=limits_values(), unexpected=True)
    with pytest.raises(ValidationError):
        request.expected_revision = 13


def test_reconciliation_anchors_keep_per_instrument_lots_and_legacy_history():
    anchors = [
        {"instrument_uid": "gold", "quantity_units": "18", "lot_size": 1},
        {"instrument_uid": "sibn", "quantity_units": "30", "lot_size": 10},
    ]
    result = admission.ReconciliationResult(**(reconciliation_values() | {"position_anchors": anchors}))
    assert result.position_anchors[1].lot_size == 10
    assert result.position_anchors[1].quantity_units == Decimal(30)
    assert admission.ReconciliationResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        admission.ReconciliationResult(**(reconciliation_values() | {"position_anchors": anchors + anchors[:1]}))

    old = reconciliation_values() | {
        "scope": "CANARY_UID",
        "canary_uid": "gold",
        "status": "CANARY_READY",
        "position_anchor_units": "18",
    }
    historical = admission.ReconciliationResult(**old)
    assert historical.position_anchor_units == Decimal(18)
    assert historical.position_anchors == ()
    assert admission.ReconciliationResult.model_validate_json(historical.model_dump_json()) == historical


@pytest.mark.parametrize(
    "changes",
    [{"lot_size": v} for v in (0, -1, True, "10", 1.5)]
    + [{"quantity_units": v} for v in ("-1", "NaN", "Infinity", True, 1.5)]
    + [{"instrument_uid": v} for v in ("", "u" * 129, None)],
)
def test_position_anchor_rejects_unknown_or_unsafe_units_and_lots(changes):
    with pytest.raises(ValidationError):
        admission.ReconciliationPositionAnchor(
            **({"instrument_uid": "uid", "quantity_units": "0", "lot_size": 10} | changes)
        )


def test_permit_processing_metadata_is_additive_and_does_not_change_bindings_or_deadline():
    old = admission.SubmitPermit(**permit_values())
    assert old.server_processing_ms == 0
    current = admission.SubmitPermit(**(permit_values() | {"server_processing_ms": 250}))
    assert current.expires_at == old.expires_at
    assert current.request_fingerprint == old.request_fingerprint
    assert current.runtime_instance_id == old.runtime_instance_id
    assert admission.SubmitPermit.model_validate_json(current.model_dump_json()) == current
    with pytest.raises(ValidationError):
        admission.SubmitPermit(
            **(permit_values() | {"server_response_at": current.expires_at, "server_processing_ms": 250})
        )
    # The worker validates processing against its measured RTT; metadata never extends expiry.
    oversized_metadata = admission.SubmitPermit(**(permit_values() | {"server_processing_ms": 10000}))
    assert oversized_metadata.expires_at == old.expires_at


@pytest.mark.parametrize("value", [-1, True, "1", 1.5, None])
def test_permit_processing_metadata_is_a_nonnegative_strict_integer(value):
    with pytest.raises(ValidationError):
        admission.SubmitPermit(**(permit_values() | {"server_processing_ms": value}))


@pytest.mark.parametrize("reason", ["LIMITS_NOT_CONFIGURED", "AUTOMATION_NOT_ACTIVE", "EXPOSURE_EXCEEDED"])
def test_definitive_local_denial_round_trips_with_safe_timings(reason):
    state = BrokerOrderState(
        **(
            state_values()
            | {"submit_denial_reason": reason, "submit_authorization_elapsed_ms": 750, "permit_remaining_ms": -12}
        )
    )
    assert state.submit_denial_reason.value == reason
    assert state.permit_remaining_ms == -12
    assert BrokerOrderState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "UNCERTAIN"},
        {"status": "FILLED"},
        {"broker_order_id": "broker-order"},
        {"executed_lots": 1},
        {"executed_amount": "1"},
        {"executed_commission": "1"},
        {"executed_price": "1"},
        {"executed_at": NOW},
        {"requested_lots": 0},
        {"requested_amount": "0"},
        {"submit_denial_reason": "ALREADY_CONSUMED"},
        {"requested_amount": "Infinity"},
        {"executed_amount": "NaN"},
        {"submit_denial_reason": "raw sensitive message"},
    ],
)
def test_definitive_denial_cannot_claim_no_send_for_broker_or_uncertain_execution(changes):
    with pytest.raises(ValidationError):
        BrokerOrderState(**(state_values() | {"submit_denial_reason": "EXPOSURE_EXCEEDED"} | changes))


@pytest.mark.parametrize(
    "changes",
    [{"submit_authorization_elapsed_ms": v} for v in (-1, True, "1", 1.5)]
    + [{"permit_remaining_ms": v} for v in (True, "1", 1.5)],
)
def test_broker_timing_metadata_uses_strict_integer_milliseconds(changes):
    with pytest.raises(ValidationError):
        BrokerOrderState(**(state_values() | changes))


def test_legacy_broker_state_serialization_has_no_new_null_fields():
    old = BrokerOrderState(**state_values())
    encoded = old.model_dump(mode="json")
    assert "submit_denial_reason" not in encoded
    assert "submit_authorization_elapsed_ms" not in encoded
    assert "permit_remaining_ms" not in encoded
    assert BrokerOrderState.model_validate_json(old.model_dump_json()) == old
