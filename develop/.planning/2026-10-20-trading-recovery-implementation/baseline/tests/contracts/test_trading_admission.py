from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from importlib.util import find_spec
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from sentinel_contracts.broker_execution import BrokerConnection, BrokerOrderState, OrderSide
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionLimits,
    AdmissionStatus,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    PreRpcAbortProof,
    ReconciliationRequest,
    ReconciliationResult,
    ReconciliationStatus,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    RuntimeRegistrationStatus,
    SubmitAbsenceBasis,
    SubmitAttemptRequest,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    SubmitReservationState,
    submit_request_fingerprint,
)
from sentinel_contracts.trading_facts import (
    BrokerOrderStateChangedPayload,
    FactBrokerOrderStatus,
    FactBrokerOrderType,
    FactOrderIntentKind,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
FINGERPRINT = "a" * 64


def proof() -> PreRpcAbortProof:
    return PreRpcAbortProof(
        permit_id=UUID("00000000-0000-4000-8000-000000000101"),
        idempotency_key="00000000-0000-4000-8000-000000000102",
        request_fingerprint=FINGERPRINT,
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000103"),
        generation=1,
        configuration_fingerprint=FINGERPRINT,
        aborted_at=NOW,
        reason="PERMIT_EXPIRED_BEFORE_POST",
    )


def baseline(**changes: object) -> AdmissionBaseline:
    values: dict[str, object] = {
        "baseline_id": UUID("00000000-0000-4000-8000-000000000111"),
        "broker_id": UUID("00000000-0000-4000-8000-000000000112"),
        "account_id": "account-1",
        "moscow_day": date(2026, 10, 9),
        "timezone": "Europe/Moscow",
        "currency": "RUB",
        "equity": Decimal("10000"),
        "net_external_cash_flow": Decimal("0"),
        "capture_started_at": NOW - timedelta(seconds=1),
        "capture_finished_at": NOW,
        "source": "GET_PORTFOLIO",
        "operations_through": NOW,
        "accepted_by": "operator",
        "accepted_at": NOW,
        "policy": "FIRST_ACCEPTED_ACCOUNT_EQUITY",
    }
    values.update(changes)
    return AdmissionBaseline(**values)


def order_state_fact(**changes: object) -> BrokerOrderStateChangedPayload:
    payload: dict[str, object] = {
        "order_id": UUID("00000000-0000-4000-8000-000000000401"),
        "decision_id": UUID("00000000-0000-4000-8000-000000000402"),
        "position_cycle_id": None,
        "instrument_id": UUID("00000000-0000-4000-8000-000000000403"),
        "idempotency_key": str(proof().idempotency_key),
        "external_order_id": None,
        "intent_kind": FactOrderIntentKind.OPEN,
        "side": OrderSide.BUY,
        "order_type": FactBrokerOrderType.LIMIT,
        "state": FactBrokerOrderStatus.CANCELLED,
        "quantity_lots": 1,
        "limit_price": Decimal("100"),
        "requested_amount": Decimal("100"),
        "executed_amount": Decimal("0"),
        "estimated_commission": Decimal("1"),
        "executed_commission": Decimal("0"),
        "strategy_snapshot": {},
        "dispatch_started_at": NOW,
        "broker_responded_at": None,
        "executed_at": None,
        "terminal_at": NOW,
        "updated_at": NOW,
        "order_event_id": UUID("00000000-0000-4000-8000-000000000404"),
        "broker_order_id": UUID("00000000-0000-4000-8000-000000000405"),
        "from_state": FactBrokerOrderStatus.SUBMITTING,
        "to_state": FactBrokerOrderStatus.CANCELLED,
        "safe_reason": "CANCELLED",
        "safe_message": "Not sent",
        "occurred_at": NOW,
        "created_at": NOW,
        "pre_rpc_abort_proof": proof(),
    }
    payload.update(changes)
    return BrokerOrderStateChangedPayload(**payload)


def test_trading_admission_contract_module_exists() -> None:
    assert find_spec("sentinel_contracts.trading_admission") is not None


def test_execution_contracts_expose_additive_admission_fingerprints_and_proofs() -> None:
    assert "configuration_fingerprint" in BrokerConnection.model_fields
    assert "pre_rpc_abort_proof" in BrokerOrderState.model_fields
    assert "pre_rpc_abort_proof" in BrokerOrderStateChangedPayload.model_fields


def test_admission_limits_require_one_canary_and_finite_positive_money() -> None:
    valid = AdmissionLimits(
        canary_uid="TCS@TQBR",
        currency="RUB",
        max_order_notional="1000.00",
        exposure_scope="CANARY_UID_GROSS",
        max_exposure=Decimal("5000"),
        max_outstanding_orders=2,
        max_daily_loss=Decimal("500"),
    )
    assert valid.max_order_notional == Decimal("1000.00")

    for amount in (100.5, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValidationError):
            AdmissionLimits(
                canary_uid="TCS@TQBR",
                currency="RUB",
                max_order_notional=amount,
                exposure_scope="CANARY_UID_GROSS",
                max_exposure=Decimal("5000"),
                max_outstanding_orders=2,
                max_daily_loss=Decimal("500"),
            )

    with pytest.raises(ValidationError):
        AdmissionLimits(
            canary_uid="TCS@TQBR",
            currency="RUB",
            max_order_notional=Decimal("1000"),
            exposure_scope="CANARY_UID_GROSS",
            max_exposure=Decimal("5000"),
            max_outstanding_orders=True,
            max_daily_loss=Decimal("500"),
        )

    with pytest.raises(ValidationError):
        AdmissionLimits(
            currency="RUB",
            max_order_notional=Decimal("1000"),
            exposure_scope="CANARY_UID_GROSS",
            max_exposure=Decimal("5000"),
            max_outstanding_orders=2,
            max_daily_loss=Decimal("500"),
        )


def test_baseline_request_requires_owner_acceptance_and_attempt_view_preserves_unknown() -> None:
    assert CaptureBaselineRequest(day=date(2026, 10, 9), accept_policy=True)
    with pytest.raises(ValidationError):
        CaptureBaselineRequest(day=date(2026, 10, 9), accept_policy=False)

    attempt = SubmitAttemptView(
        broker_id=UUID("00000000-0000-4000-8000-000000000501"),
        account_id="account-1",
        idempotency_key=UUID("00000000-0000-4000-8000-000000000502"),
        consumed_state=SubmitReservationState.UNKNOWN,
        proven_not_granted=False,
        original_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000503"),
        original_generation=1,
        original_generation_retired=False,
        absence_basis=None,
    )
    assert attempt.consumed_state is SubmitReservationState.UNKNOWN
    with pytest.raises(ValidationError):
        SubmitDenied(
            reason=SubmitDenialReason.ALREADY_CONSUMED,
            outcome_uncertain=False,
            attempt=attempt,
        )
    with pytest.raises(ValidationError):
        SubmitDenied(
            reason=SubmitDenialReason.DISARMED,
            outcome_uncertain=False,
            attempt=attempt,
        )
    with pytest.raises(ValidationError):
        SubmitAttemptView.model_validate(attempt.model_dump() | {"proven_not_granted": True})
    proven = attempt.model_dump() | {
        "consumed_state": None,
        "proven_not_granted": True,
        "original_generation_retired": True,
        "absence_basis": SubmitAbsenceBasis.RETIRED_GENERATION,
    }
    assert SubmitAttemptView.model_validate(proven).proven_not_granted

    assert ReconciliationRequest(expected_revision=1).canary_uid is None
    assert ReconciliationRequest(expected_revision=1, canary_uid="TCS@TQBR").canary_uid == "TCS@TQBR"
    with pytest.raises(ValidationError):
        ReconciliationRequest(expected_revision=1, canary_uid="")
    assert SubmitAttemptRequest().original_generation is None
    with pytest.raises(ValidationError):
        SubmitAttemptRequest(original_generation=1)


def test_admission_request_timestamps_are_utc_millisecond_and_submit_deadline_is_required() -> None:
    with pytest.raises(ValidationError):
        baseline(capture_finished_at=NOW.astimezone(timezone(timedelta(hours=3))))

    with pytest.raises(ValidationError):
        baseline(capture_finished_at=NOW.replace(microsecond=1))

    request = SubmitAuthorizationRequest(
        broker_id=UUID("00000000-0000-4000-8000-000000000201"),
        account_id="account-1",
        automation_id=UUID("00000000-0000-4000-8000-000000000202"),
        idempotency_key="00000000-0000-4000-8000-000000000203",
        generation=1,
        revision=1,
        configuration_fingerprint=FINGERPRINT,
        instrument_uid="TCS@TQBR",
        side="BUY",
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("100"),
        market_valid_until=NOW,
        submit_deadline=NOW,
    )
    assert request.submit_deadline == NOW
    assert request.model_dump(mode="json")["currency"] == "RUB"

    payload = request.model_dump()
    del payload["submit_deadline"]
    with pytest.raises(ValidationError):
        SubmitAuthorizationRequest.model_validate(payload)

    for required_field in ("lot_size", "currency"):
        incomplete = request.model_dump()
        del incomplete[required_field]
        with pytest.raises(ValidationError):
            SubmitAuthorizationRequest.model_validate(incomplete)

    for lot_size in (True, 0, 1.5):
        with pytest.raises(ValidationError):
            SubmitAuthorizationRequest.model_validate(request.model_dump() | {"lot_size": lot_size})

    with pytest.raises(ValidationError):
        SubmitAuthorizationRequest.model_validate(request.model_dump() | {"currency": "USD"})


def test_submit_fingerprint_uses_only_canonical_immutable_economic_request() -> None:
    request = SubmitAuthorizationRequest(
        broker_id=UUID("00000000-0000-4000-8000-000000000211"),
        account_id="account-1",
        automation_id=UUID("00000000-0000-4000-8000-000000000212"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000213"),
        generation=1,
        revision=1,
        configuration_fingerprint=FINGERPRINT,
        instrument_uid="TCS@TQBR",
        side="BUY",
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("100.00"),
        market_valid_until=NOW,
        submit_deadline=NOW,
    )
    assert submit_request_fingerprint(request) == submit_request_fingerprint(
        request.model_copy(update={"limit_price": Decimal("100")})
    )
    assert submit_request_fingerprint(request) != submit_request_fingerprint(
        request.model_copy(update={"quantity_lots": 2})
    )
    assert submit_request_fingerprint(request) != submit_request_fingerprint(request.model_copy(update={"lot_size": 1}))
    assert submit_request_fingerprint(request) != submit_request_fingerprint(
        request.model_copy(update={"side": "SELL"})
    )
    assert submit_request_fingerprint(request) == "cb541b58ffe831baed1ed67c4485f95601f0c84391bdcc318907b47b3aa53dd7"
    assert submit_request_fingerprint(request) == submit_request_fingerprint(
        request.model_copy(update={"generation": 9, "revision": 10, "submit_deadline": NOW - timedelta(seconds=1)})
    )
    with pytest.raises(ValidationError):
        SubmitAuthorizationRequest.model_validate(
            request.model_dump() | {"limit_price": Decimal("10000000000000000000")}
        )
    with pytest.raises(ValidationError):
        SubmitAuthorizationRequest.model_validate(request.model_dump() | {"limit_price": Decimal("0.0000000001")})


def test_runtime_credentials_are_not_part_of_registration_body() -> None:
    request = RuntimeRegistrationRequest(
        worker_id="trading-automaton-1",
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000300"),
        broker_id=UUID("00000000-0000-4000-8000-000000000301"),
        application_environment="PROD",
        access_mode="READ_ONLY",
        strategy_enabled=False,
    )
    result = RuntimeRegistrationResult(
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000302"),
        generation=None,
        registration_status=RuntimeRegistrationStatus.PENDING,
        admission_status=AdmissionStatus.DISARMED,
        lease_expires_at=None,
    )
    assert "runtime_secret" not in RuntimeRegistrationRequest.model_fields
    assert result.registration_status is RuntimeRegistrationStatus.PENDING
    with pytest.raises(ValidationError):
        RuntimeRegistrationResult.model_validate(result.model_dump() | {"generation": 1})
    with pytest.raises(ValidationError):
        RuntimeRegistrationResult.model_validate(result.model_dump() | {"lease_expires_at": NOW})
    with pytest.raises(ValidationError):
        RuntimeRegistrationResult.model_validate(
            result.model_dump()
            | {
                "registration_status": RuntimeRegistrationStatus.OWNER,
                "generation": None,
                "lease_expires_at": NOW,
            }
        )
    assert (
        RuntimeRegistrationResult(
            runtime_instance_id=UUID("00000000-0000-4000-8000-000000000303"),
            generation=1,
            registration_status=RuntimeRegistrationStatus.OWNER,
            admission_status=AdmissionStatus.DISARMED,
            lease_expires_at=NOW,
        ).generation
        == 1
    )
    with pytest.raises(ValidationError):
        RuntimeRegistrationRequest.model_validate(request.model_dump() | {"runtime_" + "secret": "synthetic-value"})


def test_owner_stop_is_explicit_and_reconciliation_binds_its_scope() -> None:
    stopped = OwnerStoppedRequest(
        expected_revision=2,
        old_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000601"),
        old_generation=1,
        candidate_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000602"),
        stopped_at=NOW,
        auto_restart_disabled=True,
        evidence_ref="operator-ticket-42",
    )
    assert stopped.old_generation == 1
    with pytest.raises(ValidationError):
        OwnerStoppedRequest.model_validate(stopped.model_dump() | {"auto_restart_disabled": False})

    reconciliation = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000603"),
        broker_id=UUID("00000000-0000-4000-8000-000000000604"),
        account_id="account-1",
        scope="CANARY_UID",
        owner_runtime_instance_id=stopped.candidate_runtime_instance_id,
        owner_generation=2,
        configuration_fingerprint=FINGERPRINT,
        status=ReconciliationStatus.CANARY_READY,
        canary_uid="TCS@TQBR",
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        can_arm=True,
        position_anchor_units=Decimal("0"),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000605"),
    )
    assert reconciliation.can_arm
    with pytest.raises(ValidationError):
        ReconciliationResult.model_validate(reconciliation.model_dump() | {"scope": "ACCOUNT"})


def test_admission_view_exposes_current_evidence_and_effective_gate() -> None:
    reconciliation = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000701"),
        broker_id=UUID("00000000-0000-4000-8000-000000000702"),
        account_id="account-1",
        scope="ACCOUNT",
        owner_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000703"),
        owner_generation=1,
        configuration_fingerprint=FINGERPRINT,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        can_arm=False,
        position_anchor_units=None,
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(SubmitDenialReason.RECONCILIATION_REQUIRED,),
        risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000704"),
    )
    view = AdmissionView(
        broker_id=reconciliation.broker_id,
        account_id=reconciliation.account_id,
        revision=1,
        owner_runtime_instance_id=reconciliation.owner_runtime_instance_id,
        owner_generation=1,
        recovery_runtime_instance_id=None,
        admission_status=AdmissionStatus.DISARMED,
        arm_revision=None,
        lease_expires_at=NOW + timedelta(seconds=3),
        arm_expires_at=None,
        limits=None,
        configuration_fingerprint=FINGERPRINT,
        baseline=baseline(),
        reconciliation=reconciliation,
        effective_can_submit=False,
        effective_denial_reasons=(SubmitDenialReason.RECONCILIATION_REQUIRED,),
        unresolved_reservation_count=0,
        outstanding_order_count=0,
        recovery_only=False,
    )
    assert not view.effective_can_submit
    assert view.reconciliation == reconciliation


def test_arm_requires_explicit_limits_and_revision() -> None:
    with pytest.raises(ValidationError):
        ArmRequest(expected_revision=1)

    limits = AdmissionLimits(
        canary_uid="TCS@TQBR",
        currency="RUB",
        max_order_notional=Decimal("1000"),
        exposure_scope="CANARY_UID_GROSS",
        max_exposure=Decimal("5000"),
        max_outstanding_orders=1,
        max_daily_loss=Decimal("500"),
    )
    arm = ArmRequest(
        expected_revision=1,
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000801"),
        baseline_id=UUID("00000000-0000-4000-8000-000000000802"),
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000803"),
        limits=limits,
        configuration_fingerprint=FINGERPRINT,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert arm.expires_at > NOW


def test_submit_result_is_discriminated_and_permit_has_server_timing() -> None:
    permit = SubmitPermit(
        permit_id=UUID("00000000-0000-4000-8000-000000000901"),
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000902"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000903"),
        generation=1,
        revision=2,
        configuration_fingerprint=FINGERPRINT,
        request_fingerprint=FINGERPRINT,
        issued_at=NOW,
        expires_at=NOW + timedelta(milliseconds=500),
        server_response_at=NOW + timedelta(milliseconds=100),
        risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000904"),
    )
    adapter = TypeAdapter(SubmitAuthorizationResult)
    assert isinstance(adapter.validate_python(permit.model_dump(mode="json")), SubmitPermit)

    denied = SubmitDenied(reason=SubmitDenialReason.DISARMED, outcome_uncertain=False)
    assert adapter.validate_python(denied.model_dump(mode="json")).result == "DENIED"
    with pytest.raises(ValidationError):
        SubmitDenied(reason=SubmitDenialReason.ALREADY_CONSUMED, outcome_uncertain=False)
    with pytest.raises(ValidationError):
        SubmitPermit.model_validate(permit.model_dump() | {"server_response_at": permit.expires_at})


def test_pre_rpc_abort_proof_is_only_valid_for_zero_fill_cancelled_state() -> None:
    with pytest.raises(ValidationError):
        PreRpcAbortProof.model_validate(proof().model_dump() | {"reason": "ADMISSION_EXPIRED_BEFORE_RPC"})

    state = BrokerOrderState(
        broker_order_id="",
        idempotency_key=str(proof().idempotency_key),
        status="CANCELLED",
        requested_lots=1,
        executed_lots=0,
        requested_amount=Decimal("100"),
        executed_amount=Decimal("0"),
        estimated_commission=Decimal("1"),
        executed_commission=Decimal("0"),
        currency="RUB",
        pre_rpc_abort_proof=proof(),
    )
    assert state.pre_rpc_abort_proof == proof()

    with pytest.raises(ValidationError):
        BrokerOrderState.model_validate(state.model_dump() | {"executed_amount": Decimal("1")})

    with pytest.raises(ValidationError):
        BrokerOrderState.model_validate(state.model_dump() | {"status": "UNKNOWN"})
    with pytest.raises(ValidationError):
        BrokerOrderState.model_validate(state.model_dump() | {"requested_lots": 0})

    fact = order_state_fact()
    assert fact.pre_rpc_abort_proof == proof()
    with pytest.raises(ValidationError):
        order_state_fact(executed_amount=Decimal("1"))
    with pytest.raises(ValidationError):
        order_state_fact(quantity_lots=0)

    assert fact.model_dump(mode="json")["pre_rpc_abort_proof"]["reason"] == "PERMIT_EXPIRED_BEFORE_POST"


def test_legacy_order_json_round_trips_without_new_null_proof_keys() -> None:
    state = BrokerOrderState(
        "order-1",
        "intent-1",
        "ACCEPTED",
        1,
        0,
        Decimal("100"),
        Decimal(0),
        Decimal(1),
        Decimal(0),
        "RUB",
    )
    old_state_json = state.model_dump(mode="json", exclude={"pre_rpc_abort_proof"})
    assert old_state_json["executed_at"] is None
    assert BrokerOrderState.model_validate(old_state_json).model_dump(mode="json") == old_state_json

    old_fact_json = order_state_fact(pre_rpc_abort_proof=None).model_dump(mode="json", exclude={"pre_rpc_abort_proof"})
    assert old_fact_json["external_order_id"] is None
    assert BrokerOrderStateChangedPayload.model_validate(old_fact_json).model_dump(mode="json") == old_fact_json


def test_admission_models_are_frozen_and_reject_unknown_fields() -> None:
    denied = SubmitDenied(
        reason=SubmitDenialReason.DISARMED,
        outcome_uncertain=False,
    )
    with pytest.raises(ValidationError):
        denied.outcome_uncertain = True

    with pytest.raises(ValidationError):
        SubmitDenied.model_validate(
            {"reason": "DISARMED", "outcome_uncertain": False, "sec" + "ret": "synthetic-value"}
        )


def test_legacy_execution_json_still_loads_without_admission_fields() -> None:
    connection = BrokerConnection("broker-1", "TINVEST_SANDBOX", "sandbox", "token", True)
    state = BrokerOrderState(
        "order-1",
        "intent-1",
        "ACCEPTED",
        1,
        0,
        Decimal("100"),
        Decimal(0),
        Decimal(1),
        Decimal(0),
        "RUB",
    )
    assert connection.configuration_fingerprint is None
    assert state.pre_rpc_abort_proof is None
    assert OrderSide.BUY.value == "BUY"
