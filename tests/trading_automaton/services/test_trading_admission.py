import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    AdmissionStatus,
    AdmissionView,
    SubmitAuthorizationRequest,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    submit_request_fingerprint,
)
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.services.trading_admission import TradingAdmissionService

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
FINGERPRINT = "a" * 64


class Core:
    def __init__(self, view=None, error=None, authorization=None):
        self.view = view
        self.error = error
        self.authorization = authorization

    def trading_admission(self, broker_id):
        if self.error is not None:
            raise self.error
        assert broker_id == str(BROKER_ID)
        return self.view

    def authorize_submit(self, request):
        if self.error is not None:
            raise self.error
        return self.authorization(request)


def view(**changes):
    values = {
        "broker_id": BROKER_ID,
        "account_id": "account",
        "revision": 5,
        "owner_runtime_instance_id": RUNTIME_ID,
        "owner_generation": 2,
        "recovery_runtime_instance_id": None,
        "admission_status": AdmissionStatus.ARMED,
        "arm_revision": 4,
        "lease_expires_at": NOW + timedelta(minutes=1),
        "arm_expires_at": NOW + timedelta(minutes=1),
        "limits": AdmissionLimits(
            canary_uid="uid-1",
            currency="RUB",
            max_order_notional="1000",
            exposure_scope="CANARY_UID_GROSS",
            max_exposure="2000",
            max_outstanding_orders=1,
            max_daily_loss="100",
        ),
        "configuration_fingerprint": FINGERPRINT,
        "baseline": None,
        "reconciliation": None,
        "effective_can_submit": True,
        "effective_denial_reasons": (),
        "unresolved_reservation_count": 0,
        "outstanding_order_count": 0,
        "recovery_only": False,
    }
    values.update(changes)
    return AdmissionView(**values)


def service(core, monotonic=lambda: 10.0):
    return TradingAdmissionService(
        core,
        broker_id=BROKER_ID,
        account_id="account",
        runtime_instance_id=RUNTIME_ID,
        configuration_fingerprint=FINGERPRINT,
        now=lambda: NOW,
        monotonic=monotonic,
    )


@pytest.mark.parametrize(
    ("current", "uid", "expected"),
    [
        (view(), "uid-1", None),
        (view(effective_can_submit=False), "uid-1", "DISARMED"),
        (view(), "uid-2", None),
        (view(configuration_fingerprint="b" * 64), "uid-1", "CONFIGURATION_CHANGED"),
        (view(recovery_only=True), "uid-1", None),
        (view(recovery_only=True, owner_runtime_instance_id=uuid4()), "uid-1", "NOT_OWNER"),
        (view(owner_generation=None), "uid-1", "NOT_OWNER"),
        (view(owner_runtime_instance_id=uuid4()), "uid-1", "NOT_OWNER"),
    ],
)
def test_prepare_requires_current_owner_and_technical_readiness(current, uid, expected):
    result = asyncio.run(service(Core(current)).prepare(uid))
    assert (None if isinstance(result, AdmissionView) else result.value) == expected


def test_core_failure_prevents_intent_preparation():
    result = asyncio.run(service(Core(error=TimeoutError())).prepare("uid-1"))
    assert result.value == "CORE_UNAVAILABLE"


def test_prepare_accepts_any_automation_uid_without_arm_or_canary_binding():
    account = view(
        admission_status=AdmissionStatus.DISARMED,
        arm_revision=None,
        arm_expires_at=None,
        baseline=None,
        reconciliation=None,
        recovery_only=False,
        effective_can_submit=True,
        effective_denial_reasons=(),
    )

    result = asyncio.run(service(Core(account)).prepare("uid-2"))

    assert result is account


def test_prepare_ignores_legacy_recovery_only_marker():
    account = view(recovery_only=True, effective_can_submit=True, effective_denial_reasons=())

    result = asyncio.run(service(Core(account)).prepare("uid-1"))

    assert result is account


def test_prepare_allows_core_on_demand_baseline_fallback_only_for_sole_baseline_reason():
    account = view(
        baseline=None,
        effective_can_submit=False,
        effective_denial_reasons=(SubmitDenialReason.BASELINE_REQUIRED,),
    )

    result = asyncio.run(service(Core(account)).prepare("uid-2"))

    assert result is account


@pytest.mark.parametrize(
    "reasons",
    [
        (SubmitDenialReason.BASELINE_REQUIRED, SubmitDenialReason.DATA_UNKNOWN),
        (SubmitDenialReason.DATA_UNKNOWN, SubmitDenialReason.BASELINE_REQUIRED),
        (SubmitDenialReason.BASELINE_REQUIRED, SubmitDenialReason.NOT_OWNER),
    ],
)
def test_prepare_does_not_bypass_baseline_when_another_core_reason_is_present(reasons):
    account = view(baseline=None, effective_can_submit=False, effective_denial_reasons=reasons)

    result = asyncio.run(service(Core(account)).prepare("uid-2"))

    assert result is reasons[0]


@pytest.mark.parametrize(
    "account",
    [view(owner_runtime_instance_id=uuid4()), view(owner_generation=None)],
)
def test_prepare_still_requires_current_runtime_owner(account):
    result = asyncio.run(service(Core(account)).prepare("uid-1"))

    assert result is SubmitDenialReason.NOT_OWNER


def test_submit_grant_validates_fingerprint_identity_and_conservative_http_rtt():
    authorized = []

    def authorize(request: SubmitAuthorizationRequest) -> SubmitPermit:
        authorized.append(request)
        assert request.submit_deadline <= request.market_valid_until
        return SubmitPermit(
            permit_id=uuid4(),
            runtime_instance_id=RUNTIME_ID,
            idempotency_key=request.idempotency_key,
            generation=request.generation,
            revision=request.revision,
            configuration_fingerprint=FINGERPRINT,
            request_fingerprint=submit_request_fingerprint(request),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            server_response_at=NOW,
            risk_snapshot_id=uuid4(),
        )

    async def scenario():
        samples = iter((10.0, 10.2))
        gate = service(Core(authorization=authorize), monotonic=lambda: next(samples))
        return await gate.authorize_submit(
            DispatchRequest(
                idempotency_key="00000000-0000-4000-8000-000000000003",
                account_id="account",
                instrument_id="uid-1",
                side=OrderSide.BUY,
                quantity_lots=2,
                limit_price=Decimal("10.50"),
                automation_id="00000000-0000-4000-8000-000000000004",
                lot_size=10,
                reservation_currency="RUB",
                broker_id=str(BROKER_ID),
                market_valid_until=NOW + timedelta(seconds=2),
                admission_runtime_instance_id=str(RUNTIME_ID),
                admission_generation=2,
                admission_revision=5,
                configuration_fingerprint=FINGERPRINT,
            )
        )

    result = asyncio.run(scenario())
    assert authorized[0].lot_size == 10
    assert authorized[0].currency == "RUB"
    assert result.permit.request_fingerprint == submit_request_fingerprint(
        SubmitAuthorizationRequest(
            broker_id=BROKER_ID,
            account_id="account",
            automation_id=UUID("00000000-0000-4000-8000-000000000004"),
            idempotency_key=UUID("00000000-0000-4000-8000-000000000003"),
            generation=2,
            revision=5,
            configuration_fingerprint=FINGERPRINT,
            instrument_uid="uid-1",
            lot_size=10,
            currency="RUB",
            side="BUY",
            quantity_lots=2,
            limit_price=Decimal("10.50"),
            market_valid_until=NOW + timedelta(seconds=2),
            submit_deadline=NOW + timedelta(seconds=1),
        )
    )
    assert result.expires_monotonic == pytest.approx(11.0)


def test_known_core_denial_returns_no_grant_and_uncertain_denial_raises():
    known = SubmitDenied(reason=SubmitDenialReason.DISARMED, outcome_uncertain=False)
    gate = service(Core(authorization=lambda _: known))
    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=NOW + timedelta(seconds=2),
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    )
    assert asyncio.run(gate.authorize_submit(request)).denial == known


def test_expired_prod_submit_still_checks_core_consumption_before_local_denial():
    requests = []
    expired_at = NOW - timedelta(seconds=1)
    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=expired_at,
        submit_deadline=expired_at,
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    )
    known_stale = SubmitDenied(reason=SubmitDenialReason.STALE_MARKET, outcome_uncertain=False)

    def authorize(value):
        requests.append(value)
        return known_stale

    admission = TradingAdmissionService(
        Core(authorization=authorize),
        broker_id=BROKER_ID,
        account_id="account",
        runtime_instance_id=RUNTIME_ID,
        configuration_fingerprint=FINGERPRINT,
        now=lambda: NOW,
    )

    result = asyncio.run(admission.authorize_submit(request))

    assert result.denial == known_stale
    assert result.abort_proof is None
    assert len(requests) == 1
    assert requests[0].idempotency_key == UUID(request.idempotency_key)
    assert requests[0].submit_deadline <= requests[0].market_valid_until


def test_slow_core_round_trip_exhausts_permit_and_returns_proven_abort():
    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=NOW + timedelta(seconds=2),
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    )

    def authorize(value):
        return SubmitPermit(
            permit_id=uuid4(),
            runtime_instance_id=RUNTIME_ID,
            idempotency_key=value.idempotency_key,
            generation=value.generation,
            revision=value.revision,
            configuration_fingerprint=FINGERPRINT,
            request_fingerprint=submit_request_fingerprint(value),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            server_response_at=NOW,
            risk_snapshot_id=uuid4(),
        )

    samples = iter((10.0, 11.2))
    result = asyncio.run(
        service(Core(authorization=authorize), monotonic=lambda: next(samples)).authorize_submit(request)
    )

    assert result.permit is not None
    assert result.expires_monotonic is None
    assert result.abort_proof is not None
    assert result.abort_proof.reason == "PERMIT_EXPIRED_BEFORE_POST"


def test_server_processing_metadata_is_subtracted_from_round_trip_once():
    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=NOW + timedelta(seconds=2),
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    )

    def authorize(value):
        return SubmitPermit(
            permit_id=uuid4(),
            runtime_instance_id=RUNTIME_ID,
            idempotency_key=value.idempotency_key,
            generation=value.generation,
            revision=value.revision,
            configuration_fingerprint=FINGERPRINT,
            request_fingerprint=submit_request_fingerprint(value),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            server_response_at=NOW,
            server_processing_ms=800,
            risk_snapshot_id=uuid4(),
        )

    samples = iter((10.0, 11.2))
    result = asyncio.run(
        service(Core(authorization=authorize), monotonic=lambda: next(samples)).authorize_submit(request)
    )

    assert result.abort_proof is None
    assert result.expires_monotonic == pytest.approx(11.8)


def test_processing_metadata_greater_than_rtt_falls_back_to_full_rtt():
    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=NOW + timedelta(seconds=2),
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    )

    def authorize(value):
        return SubmitPermit(
            permit_id=uuid4(),
            runtime_instance_id=RUNTIME_ID,
            idempotency_key=value.idempotency_key,
            generation=value.generation,
            revision=value.revision,
            configuration_fingerprint=FINGERPRINT,
            request_fingerprint=submit_request_fingerprint(value),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            server_response_at=NOW,
            server_processing_ms=1500,
            risk_snapshot_id=uuid4(),
        )

    samples = iter((10.0, 11.2))
    result = asyncio.run(
        service(Core(authorization=authorize), monotonic=lambda: next(samples)).authorize_submit(request)
    )

    assert result.abort_proof is not None
    assert result.abort_proof.reason == "PERMIT_EXPIRED_BEFORE_POST"


@pytest.mark.parametrize(
    ("field", "value"),
    [("broker_id", str(uuid4())), ("account_id", "other-account")],
)
def test_submit_request_scope_must_match_configured_broker(field, value):
    calls = []

    def authorize(request):
        calls.append(request)
        return SubmitDenied(reason=SubmitDenialReason.NOT_OWNER, outcome_uncertain=False)

    request = DispatchRequest(
        idempotency_key="00000000-0000-4000-8000-000000000003",
        account_id="account",
        instrument_id="uid-1",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id="00000000-0000-4000-8000-000000000004",
        lot_size=1,
        reservation_currency="RUB",
        broker_id=str(BROKER_ID),
        market_valid_until=NOW + timedelta(seconds=2),
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=2,
        admission_revision=5,
        configuration_fingerprint=FINGERPRINT,
    ).model_copy(update={field: value})

    with pytest.raises(CoreOperationError):
        asyncio.run(service(Core(authorization=authorize)).authorize_submit(request))
    assert calls == []
