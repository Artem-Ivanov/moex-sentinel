import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from moex_sentinel.domain.trading_admission import broker_configuration_fingerprint
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOrder,
    AdmissionOrderEstimate,
    AdmissionOrderState,
    AdmissionPosition,
    AdmissionRiskSnapshot,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base, TradingAutomationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderEventModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.trading_admission import (
    AdmissionConflictError,
    AdmissionReservationRecord,
    TradingAdmissionRepository,
)
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionLimits,
    AdmissionStatus,
    ArmRequest,
    OwnerStoppedRequest,
    PendingRuntimeCandidate,
    ReconciliationPositionAnchor,
    ReconciliationResult,
    ReconciliationStatus,
    RevokeRequest,
    RuntimeRegistrationRequest,
    RuntimeRegistrationStatus,
    SubmitAuthorizationRequest,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    UpdateLimitsRequest,
)
from tests.storage.trading_facts_helpers import (
    automation_model,
    decision_model,
    instrument_model,
    order_model,
    position_cycle_model,
    user_broker_model,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_A = UUID("00000000-0000-4000-8000-000000000002")
RUNTIME_B = UUID("00000000-0000-4000-8000-000000000003")
SECRET_A = "a" * 64
SECRET_B = "b" * 64


@pytest.fixture
def core_engine():
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def admission_service(core_engine):
    factory = create_session_factory(core_engine)
    with factory.begin() as session:
        broker = user_broker_model(str(BROKER_ID), "synthetic-account")
        broker.environment = "PROD"
        broker.fqdn = "invest-public-api.tbank.ru:443"
        session.add(broker)
    return TradingAdmissionService(TradingAdmissionRepository(factory), clock=lambda: NOW)


def request(runtime_id: UUID, *, access_mode: str = "READ_ONLY") -> RuntimeRegistrationRequest:
    return RuntimeRegistrationRequest(
        worker_id="worker-prod-1",
        runtime_instance_id=runtime_id,
        broker_id=BROKER_ID,
        application_environment="PROD",
        access_mode=access_mode,
        strategy_enabled=False,
    )


def test_first_read_only_runtime_owns_scope_and_later_candidate_stays_pending(admission_service, core_engine):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    pending = admission_service.register_runtime(request(RUNTIME_B), SECRET_B)

    assert owner.registration_status is RuntimeRegistrationStatus.OWNER
    assert owner.generation == 1
    assert owner.admission_status is AdmissionStatus.DISARMED
    assert owner.lease_expires_at is not None
    assert pending.registration_status is RuntimeRegistrationStatus.PENDING
    assert pending.generation is None
    assert pending.lease_expires_at is None
    with core_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM worker_runtime_instances WHERE secret_digest IN (?, ?)",
                (SECRET_A, SECRET_B),
            ).scalar_one()
            == 0
        )


def test_trade_first_registration_never_invents_an_owner_generation(admission_service):
    result = admission_service.register_runtime(request(RUNTIME_A, access_mode="TRADE"), SECRET_A)
    assert result.registration_status is RuntimeRegistrationStatus.PENDING
    assert result.generation is None
    assert result.lease_expires_at is None


@pytest.mark.parametrize(
    ("unknown_reason", "position_units", "expected_status", "expected_reservation_state"),
    [
        ("REQUEST_NOT_FOUND", Decimal(0), ReconciliationStatus.ACCOUNT_ONLY, "SETTLED"),
        ("REQUEST_LOOKUP_UNSUPPORTED", Decimal(0), ReconciliationStatus.DATA_UNKNOWN, "UNKNOWN"),
        ("REQUEST_NOT_FOUND", Decimal(10), ReconciliationStatus.DATA_UNKNOWN, "UNKNOWN"),
    ],
)
def test_account_reconcile_settles_only_exact_age_policy_unknown_with_matching_ledger(
    core_engine, unknown_reason, position_units, expected_status, expected_reservation_state
):
    factory = create_session_factory(core_engine)
    broker_id = str(BROKER_ID)
    key = str(uuid4())
    created_at = NOW - timedelta(hours=25)
    expired_at = NOW
    captured_at = NOW
    with factory.begin() as session:
        broker = user_broker_model(broker_id, "synthetic-account")
        broker.environment = "PROD"
        instrument = instrument_model("age-instrument", broker_id)
        instrument.external_instrument_id = "age-uid"
        automation = automation_model("age-automation", user_broker_id=broker_id, instrument_id=instrument.id)
        cycle = position_cycle_model("age-cycle")
        cycle.user_broker_id = broker_id
        cycle.automation_id = automation.id
        cycle.instrument_id = instrument.id
        session.add(broker)
        session.flush()
        session.add(instrument)
        session.flush()
        session.add(automation)
        session.flush()
        session.add(cycle)
        session.flush()
        decision = decision_model(
            "age-decision", user_broker_id=broker_id, automation_id=automation.id, instrument_id=instrument.id
        )
        decision.position_cycle_id = cycle.id
        session.add(decision)
        session.flush()
        order = order_model(
            "age-order",
            decision_id=decision.id,
            user_broker_id=broker_id,
            automation_id=automation.id,
            instrument_id=instrument.id,
            idempotency_key=key,
        )
        order.position_cycle_id = cycle.id
        order.state = "FAILED"
        order.created_at = created_at
        order.updated_at = captured_at
        session.add(order)
        session.flush()
        session.add(
            BrokerOrderEventModel(
                id="age-event",
                fact_id="age-event-fact",
                user_broker_id=broker_id,
                automation_id=automation.id,
                broker_order_id=order.id,
                from_state="ACCEPTED",
                to_state="FAILED",
                safe_reason="ORDER_AGE_LIMIT_EXCEEDED",
                safe_message="Order exceeded 24-hour processing limit",
                occurred_at=expired_at,
                created_at=captured_at,
            )
        )
        reservation = TradingSubmitReservationModel(
            reservation_id=str(uuid4()),
            user_broker_id=broker_id,
            automation_id=automation.id,
            idempotency_key=key,
            request_fingerprint="f" * 64,
            state="UNKNOWN",
            runtime_instance_id=str(RUNTIME_A),
            generation=1,
            configuration_fingerprint="a" * 64,
            permit_id=None,
            side="BUY",
            instrument_uid="age-uid",
            quantity_lots=1,
            limit_price=Decimal("10"),
            requested_amount=Decimal("100"),
            reserved_cash=Decimal("101"),
            reserved_position_units=Decimal(0),
            payload={
                "_age_policy": {
                    "version": 1,
                    "expired_at": expired_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                }
            },
            created_at=created_at,
            updated_at=captured_at,
        )
        session.add(reservation)
        session.add(
            TradingAccountAdmissionModel(
                user_broker_id=broker_id,
                revision=1,
                admission_status="DISARMED",
                recovery_only=False,
            )
        )
        session.flush()
        result = ReconciliationResult(
            reconciliation_id=uuid4(),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            scope="ACCOUNT",
            owner_runtime_instance_id=RUNTIME_A,
            owner_generation=1,
            configuration_fingerprint="a" * 64,
            status=ReconciliationStatus.ACCOUNT_ONLY,
            canary_uid=None,
            captured_at=captured_at,
            valid_until=captured_at + timedelta(seconds=20),
            can_arm=True,
            position_anchor_units=None,
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=1,
            reasons=(),
            risk_snapshot_id=uuid4(),
            position_anchors=(
                ReconciliationPositionAnchor(instrument_uid="age-uid", quantity_units=position_units, lot_size=10),
            ),
        )
        state = AdmissionOrderState(
            request_id=key,
            broker_order_id=None,
            instrument_uid=None,
            side=None,
            requested_lots=None,
            executed_lots=None,
            executed_amount=None,
            executed_commission=None,
            currency=None,
            status="UNKNOWN",
            attributable=False,
            unknown_reason=unknown_reason,
        )
        admission = session.get(TradingAccountAdmissionModel, broker_id)
        reconciled = TradingAdmissionRepository._reconcile_submit_reservations(
            session, admission, result, order_states=(state,)
        )
        assert reconciled.status is expected_status
        session.flush()
        assert reservation.state == expected_reservation_state
        if expected_reservation_state == "SETTLED":
            assert reservation.reserved_cash == reservation.reserved_position_units == 0
            assert reservation.payload["settlement_reason"] == "AGE_POLICY"
            assert reservation.payload["broker_outcome_unknown"] is True
            assert reservation.payload["last_reconciled_order_state"] == "UNKNOWN"
            assert reservation.payload["_age_policy"]["version"] == 1


def test_verified_late_fill_delta_matches_next_submit_position_projection():
    anchor_id = str(uuid4())
    settled = AdmissionReservationRecord(
        idempotency_key="age-order",
        state="SETTLED",
        side="BUY",
        instrument_uid="late-fill-uid",
        requested_amount=Decimal("1000"),
        reserved_cash=Decimal(0),
        reserved_position_units=Decimal(0),
        quantity_lots=1,
        runtime_instance_id=str(RUNTIME_A),
        generation=1,
        anchor_reconciliation_id=anchor_id,
        anchor_executed_lots=0,
        executed_lots=1,
        executed_amount=Decimal("1000"),
        executed_commission=Decimal("1"),
    )
    active = AdmissionReservationRecord(
        idempotency_key="next-order",
        state="IN_FLIGHT",
        side="BUY",
        instrument_uid="late-fill-uid",
        requested_amount=Decimal("1000"),
        reserved_cash=Decimal("1001"),
        reserved_position_units=Decimal(0),
        quantity_lots=1,
        runtime_instance_id=str(RUNTIME_A),
        generation=1,
        anchor_reconciliation_id=anchor_id,
        anchor_executed_lots=0,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
    )
    reconciliation = ReconciliationResult(
        reconciliation_id=UUID(anchor_id),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=1,
        configuration_fingerprint="a" * 64,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=20),
        can_arm=True,
        position_anchor_units=None,
        active_ordinary_order_count=1,
        active_stop_order_count=0,
        unresolved_reservation_count=1,
        reasons=(),
        risk_snapshot_id=uuid4(),
        position_anchors=(
            ReconciliationPositionAnchor(
                instrument_uid="late-fill-uid",
                quantity_units=Decimal(0),
                lot_size=10,
            ),
        ),
    )
    context = SimpleNamespace(reservations=(settled, active), reconciliation=reconciliation)
    order_state = AdmissionOrderState(
        request_id="next-order",
        broker_order_id="next-broker-order",
        instrument_uid="late-fill-uid",
        side=OrderSide.BUY,
        requested_lots=1,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
        currency="RUB",
        status="NEW",
        attributable=True,
        unknown_reason=None,
    )
    snapshot = SimpleNamespace(
        order_states=(order_state,),
        ordinary_orders=(
            AdmissionOrder(
                broker_order_id="next-broker-order",
                instrument_uid="late-fill-uid",
                request_id="next-order",
                status="NEW",
                side=OrderSide.BUY,
                quantity_lots=1,
            ),
        ),
    )

    projection = TradingAdmissionService._project_submit_reservations(
        context,
        snapshot,
        {"late-fill-uid": (Decimal(10), NOW)},
        {
            "late-fill-uid": AdmissionInstrument(
                "late-fill-uid", "SHARE", "RUB", 10, Decimal("0.01"), True, Decimal(100), NOW
            )
        },
    )

    assert isinstance(projection, tuple)
    assert projection[0] == settled
    assert projection[1].state == "BROKER_ACTIVE"


@pytest.mark.parametrize(
    ("hold_reason", "event_expected_revision", "automation_revision", "should_resume"),
    [
        (
            "Uncertain broker intent could not be reconciled unambiguously",
            1,
            2,
            True,
        ),
        ("Invalid worker session", 1, 2, False),
        (
            "Uncertain broker intent could not be reconciled unambiguously",
            1,
            3,
            False,
        ),
    ],
)
def test_automatic_resume_requires_exact_worker_technical_hold_and_current_revision(
    core_engine, hold_reason, event_expected_revision, automation_revision, should_resume
):
    factory = create_session_factory(core_engine)
    broker_id = str(BROKER_ID)
    automation_id = "technical-hold-automation"
    with factory.begin() as session:
        session.add(user_broker_model(broker_id, "synthetic-account"))
        session.flush()
        session.add(instrument_model("technical-hold-instrument", broker_id))
        session.flush()
        automation = automation_model(
            automation_id,
            user_broker_id=broker_id,
            instrument_id="technical-hold-instrument",
            state="HOLD",
        )
        automation.suspended_from_state = "IN_WORK"
        automation.hold_reason = hold_reason
        automation.revision = automation_revision
        session.add(automation)
        session.flush()
        session.add(
            AutomationEventModel(
                event_id=str(uuid4()),
                automation_id=automation_id,
                user_broker_id=broker_id,
                sequence_number=1,
                expected_revision=event_expected_revision,
                fact_kind="AUTOMATION_STATE_CHANGED",
                safe_message=hold_reason,
                payload={
                    "state": "HOLD",
                    "suspended_from_state": "IN_WORK",
                    "hold_reason": hold_reason,
                },
                occurred_at=NOW,
                received_at=NOW,
            )
        )
    with factory.begin() as session:
        TradingAdmissionRepository._request_worker_technical_resume(session, broker_id, now=NOW)
    with factory() as session:
        current = session.get(TradingAutomationModel, automation_id)
        assert current.state == "HOLD"
        assert current.revision == automation_revision
        assert current.resume_requested is should_resume


def test_pending_runtime_lookup_is_read_only_and_scoped(admission_service):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    pending = admission_service.register_runtime(request(RUNTIME_B, access_mode="TRADE"), SECRET_B)

    candidate = admission_service.pending_runtime_candidate(str(BROKER_ID), "worker-prod-1", NOW - timedelta(seconds=1))
    assert isinstance(candidate, PendingRuntimeCandidate)
    assert candidate.runtime_instance_id == RUNTIME_B
    assert candidate.registration_status == "PENDING"
    assert candidate.registered_at == NOW
    assert candidate.broker_id == BROKER_ID
    assert candidate.worker_id == "worker-prod-1"
    assert "secret" not in str(candidate.model_dump()).lower()
    assert pending.registration_status is RuntimeRegistrationStatus.PENDING

    assert (
        admission_service.pending_runtime_candidate(str(BROKER_ID), "different-worker", NOW - timedelta(seconds=1))
        is None
    )
    assert admission_service.pending_runtime_candidate(str(BROKER_ID), "worker-prod-1", NOW) is None


def test_pending_runtime_lookup_rejects_multiple_candidates(admission_service):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    admission_service.register_runtime(request(RUNTIME_B, access_mode="TRADE"), SECRET_B)
    admission_service.register_runtime(
        request(UUID("00000000-0000-4000-8000-000000000004"), access_mode="TRADE"), "c" * 64
    )

    with pytest.raises(AdmissionConflictError):
        admission_service.pending_runtime_candidate(str(BROKER_ID), "worker-prod-1", NOW - timedelta(seconds=1))


def test_submit_context_rejects_automation_for_another_instrument(admission_service, core_engine):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    factory = create_session_factory(core_engine)
    with factory.begin() as session:
        allowed = instrument_model("allowed-instrument", str(BROKER_ID))
        allowed.external_instrument_id = "canary-uid"
        unrelated = instrument_model("unrelated-instrument", str(BROKER_ID))
        session.add_all((allowed, unrelated))
        session.flush()
        session.add(
            automation_model(
                "00000000-0000-4000-8000-000000000004",
                user_broker_id=str(BROKER_ID),
                instrument_id=unrelated.id,
            )
        )
    with pytest.raises(ValueError, match="automation instrument does not match"):
        admission_service._repository.get_submit_context(
            str(BROKER_ID), "00000000-0000-4000-8000-000000000004", "canary-uid"
        )


def test_test_environment_repository_cannot_update_prod_admission(admission_service, core_engine):
    factory = create_session_factory(core_engine)
    with factory.begin() as session:
        session.add(
            TradingAccountAdmissionModel(
                user_broker_id=str(BROKER_ID),
                revision=1,
                admission_status="DISARMED",
                recovery_only=False,
            )
        )
    repository = TradingAdmissionRepository(factory, application_environment="TEST")
    request = UpdateLimitsRequest(
        expected_revision=1,
        limits=AdmissionLimits(
            currency="RUB",
            max_order_notional=Decimal("1000"),
            exposure_scope="ACCOUNT_GROSS",
            max_exposure=Decimal("5000"),
            max_outstanding_orders=3,
            max_daily_loss=Decimal("250"),
        ),
    )

    with pytest.raises(AdmissionConflictError):
        repository.update_limits(str(BROKER_ID), request, actor="test-operator", now=NOW)

    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        assert admission is not None
        assert admission.revision == 1
        assert admission.limits is None


def test_expects_trading_uses_work_intent_and_bound_trade_owner_without_lease_gate(admission_service, core_engine):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    factory = create_session_factory(core_engine)
    with factory.begin() as session:
        owner = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        assert owner is not None
        owner.access_mode = "TRADE"
        owner.strategy_enabled = True
        owner.lease_expires_at = NOW
        other_broker_id = "00000000-0000-4000-8000-000000000099"
        other_broker = user_broker_model(other_broker_id, "another-selected-account")
        other_broker.environment = "PROD"
        other_broker.fqdn = "invest-public-api.tbank.ru:443"
        session.add(other_broker)
        session.flush()
        other_instrument = instrument_model("other-account-instrument", other_broker_id)
        session.add(other_instrument)
        session.flush()
        session.add(
            automation_model(
                "00000000-0000-4000-8000-000000000098",
                user_broker_id=other_broker_id,
                instrument_id=other_instrument.id,
                state="IN_WORK",
            )
        )
    service = TradingAdmissionService(
        TradingAdmissionRepository(factory, application_environment="PROD"), clock=lambda: NOW
    )

    assert not service.expects_trading(BROKER_ID)
    assert not service.expects_trading(UUID("00000000-0000-4000-8000-000000000099"))

    with factory.begin() as session:
        instrument = instrument_model("expected-work-instrument", str(BROKER_ID))
        session.add(instrument)
        session.flush()
        session.add(
            automation_model(
                "00000000-0000-4000-8000-000000000004",
                user_broker_id=str(BROKER_ID),
                instrument_id=instrument.id,
                state="IN_WORK",
            )
        )
    assert service.expects_trading(BROKER_ID)

    with factory.begin() as session:
        automation = session.get(TradingAutomationModel, "00000000-0000-4000-8000-000000000004")
        assert automation is not None
        automation.state = "HOLD"
    assert not service.expects_trading(BROKER_ID)


def test_unresolved_reservations_are_checked_by_fresh_submit_settlement_not_view_gate():
    admission = SimpleNamespace(
        admission_status="ARMED",
        owner_generation=1,
        recovery_only=False,
        lease_expires_at=NOW + timedelta(seconds=30),
        arm_expires_at=NOW + timedelta(seconds=60),
        limits=AdmissionLimits(
            currency="RUB",
            max_order_notional=100,
            exposure_scope="ACCOUNT_GROSS",
            max_exposure=1000,
            max_outstanding_orders=1,
            max_daily_loss=500,
        ),
    )
    owner = SimpleNamespace(registration_status="OWNER", generation=1, access_mode="TRADE", strategy_enabled=True)
    baseline = SimpleNamespace(moscow_day=NOW.date())
    reconciliation = SimpleNamespace(can_arm=True, valid_until=NOW + timedelta(seconds=30))

    reasons = TradingAdmissionRepository._effective_reasons(
        admission, owner, baseline, reconciliation, NOW, unresolved=1
    )

    assert SubmitDenialReason.DATA_UNKNOWN not in reasons


def test_owner_stop_assigns_recovery_only_generation_without_sticky_stop_gate(admission_service):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    admission_service.register_runtime(request(RUNTIME_B), SECRET_B)
    stopped = admission_service.owner_stopped(
        str(BROKER_ID),
        OwnerStoppedRequest(
            expected_revision=1,
            old_runtime_instance_id=RUNTIME_A,
            old_generation=owner.generation,
            candidate_runtime_instance_id=RUNTIME_B,
            stopped_at=NOW,
            auto_restart_disabled=True,
            evidence_ref="operator-stop-2026-10-09",
        ),
        actor="operator",
    )

    assert stopped.owner_runtime_instance_id == RUNTIME_B
    assert stopped.owner_generation == 2
    assert stopped.recovery_only
    assert stopped.admission_status is AdmissionStatus.DISARMED

    repository = admission_service._repository
    recovery = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000020"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="CANARY_UID",
        owner_runtime_instance_id=RUNTIME_B,
        owner_generation=2,
        configuration_fingerprint=broker_configuration_fingerprint(
            account_id="synthetic-account",
            access_mode="READ_ONLY",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
        ),
        status=ReconciliationStatus.CANARY_READY,
        canary_uid="canary-uid",
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        can_arm=True,
        position_anchor_units=Decimal(0),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=None,
    )
    repository.save_reconciliation(recovery, expected_revision=stopped.revision, now=NOW)

    recovered = admission_service.view(str(BROKER_ID))
    assert recovered.recovery_only
    assert recovered.admission_status is AdmissionStatus.DISARMED


def test_owner_transfer_preserves_in_work_and_rebinds_current_candidate_configuration(admission_service, core_engine):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    candidate_request = request(RUNTIME_B, access_mode="TRADE").model_copy(update={"strategy_enabled": True})
    admission_service.register_runtime(candidate_request, SECRET_B)
    factory = create_session_factory(core_engine)
    automation_id = UUID("00000000-0000-4000-8000-000000000004")
    with factory.begin() as session:
        instrument = instrument_model("transfer-instrument", str(BROKER_ID))
        instrument.external_instrument_id = "transfer-uid"
        session.add(instrument)
        session.flush()
        session.add(
            automation_model(
                str(automation_id), user_broker_id=str(BROKER_ID), instrument_id=instrument.id, state="IN_WORK"
            )
        )
        session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A)).access_mode = "TRADE"
        session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A)).strategy_enabled = True
        broker = session.get(UserBrokerModel, str(BROKER_ID))
        fingerprint = broker_configuration_fingerprint(
            account_id=broker.external_account_id,
            access_mode="TRADE",
            adapter_code=broker.api_slug,
            broker_id=broker.id,
            environment=broker.environment,
            target=broker.fqdn,
            token=str(broker.settings.get("token", "")),
        )
        session.get(TradingAccountAdmissionModel, str(BROKER_ID)).configuration_fingerprint = fingerprint

    transferred = admission_service.owner_stopped(
        str(BROKER_ID),
        OwnerStoppedRequest(
            expected_revision=1,
            old_runtime_instance_id=RUNTIME_A,
            old_generation=owner.generation,
            candidate_runtime_instance_id=RUNTIME_B,
            stopped_at=NOW,
            auto_restart_disabled=True,
            evidence_ref="synthetic-owner-transfer",
        ),
        actor="operator",
    )
    context = admission_service._repository.get_submit_context(str(BROKER_ID), str(automation_id), "transfer-uid")

    assert transferred.owner_runtime_instance_id == RUNTIME_B
    assert context.automation_state == "IN_WORK"
    assert context.configuration_fingerprint == fingerprint
    assert context.recovery_only


def test_same_runtime_registration_is_idempotent_without_lease_resurrection(admission_service, core_engine):
    first = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with core_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE trading_account_admissions SET lease_expires_at = ? WHERE user_broker_id = ?",
            (NOW - timedelta(seconds=1), str(BROKER_ID)),
        )
        connection.exec_driver_sql(
            "UPDATE worker_runtime_instances SET lease_expires_at = ? WHERE runtime_instance_id = ?",
            (NOW - timedelta(seconds=1), str(RUNTIME_A)),
        )
    replay = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    assert replay.generation == first.generation
    assert replay.lease_expires_at == NOW - timedelta(seconds=1)


def test_automatic_daily_baseline_reuse_does_not_reset_armed_admission(admission_service, core_engine):
    repository = admission_service._repository
    factory = create_session_factory(core_engine)
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with factory.begin() as session:
        session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A)).access_mode = "TRADE"
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
    )
    accepted = AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000030"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        moscow_day=NOW.date(),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal(0),
        capture_started_at=NOW - timedelta(seconds=2),
        capture_finished_at=NOW - timedelta(seconds=1),
        source="GET_PORTFOLIO",
        operations_through=NOW - timedelta(seconds=1),
        accepted_by="operator",
        accepted_at=NOW,
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )
    repository.save_baseline(accepted, configuration_fingerprint=fingerprint)
    limits = AdmissionLimits(
        canary_uid="canary-uid",
        currency="RUB",
        max_order_notional=Decimal("10000"),
        exposure_scope="CANARY_UID_GROSS",
        max_exposure=Decimal("20000"),
        max_outstanding_orders=1,
        max_daily_loss=Decimal("5000"),
    )
    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.admission_status = AdmissionStatus.ARMED.value
        admission.arm_revision = admission.revision
        admission.arm_expires_at = NOW + timedelta(minutes=5)
        admission.limits = limits.model_dump(mode="json")
        revision = admission.revision
    assert hasattr(repository, "ensure_daily_baseline"), "Automatic acceptance must be idempotent under the DB lock"

    duplicate = accepted.model_copy(
        update={"baseline_id": UUID("00000000-0000-4000-8000-000000000031"), "equity": Decimal("1")}
    )
    result = repository.ensure_daily_baseline(
        duplicate,
        configuration_fingerprint=fingerprint,
        expected_owner_runtime_instance_id=str(RUNTIME_A),
        expected_generation=owner.generation,
        snapshot_valid_until=NOW + timedelta(seconds=10),
        final_clock=lambda: NOW,
    )

    assert result == accepted
    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        assert admission.revision == revision
        assert admission.admission_status == AdmissionStatus.ARMED.value
        assert admission.arm_revision == revision
        assert admission.arm_expires_at == NOW + timedelta(minutes=5)
        assert AdmissionLimits.model_validate(admission.limits) == limits


def test_daily_baseline_rollover_preserves_stop_required_recovery_state(admission_service, core_engine):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with core_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE worker_runtime_instances SET access_mode = ? WHERE runtime_instance_id = ?",
            ("TRADE", str(RUNTIME_A)),
        )
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
    )
    with admission_service._repository._factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.recovery_only = True
        admission.recovery_runtime_instance_id = str(RUNTIME_A)
        admission.admission_status = AdmissionStatus.STOP_REQUIRED.value
        admission.lease_expires_at = NOW + timedelta(hours=14)
        owner_model = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        owner_model.lease_expires_at = NOW + timedelta(hours=14)
    baseline = AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000040"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        moscow_day=NOW.date() + timedelta(days=1),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal(0),
        capture_started_at=NOW + timedelta(hours=12),
        capture_finished_at=NOW + timedelta(hours=13),
        source="GET_PORTFOLIO",
        operations_through=NOW + timedelta(hours=13),
        accepted_by="daily-preparation",
        accepted_at=NOW + timedelta(hours=13),
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )

    accepted = admission_service._repository.ensure_daily_baseline(
        baseline,
        configuration_fingerprint=fingerprint,
        expected_owner_runtime_instance_id=str(RUNTIME_A),
        expected_generation=owner.generation,
        snapshot_valid_until=NOW + timedelta(hours=14),
        final_clock=lambda: NOW + timedelta(hours=13, minutes=30),
    )

    assert accepted == baseline
    with admission_service._repository._factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        assert admission.admission_status == AdmissionStatus.DISARMED.value
        assert admission.recovery_only is True
        assert admission.recovery_runtime_instance_id == str(RUNTIME_A)


def test_daily_preparation_accepts_baseline_but_skips_canary_in_recovery(admission_service, core_engine):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with core_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE worker_runtime_instances SET access_mode = ? WHERE runtime_instance_id = ?",
            ("TRADE", str(RUNTIME_A)),
        )
    with admission_service._repository._factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.recovery_only = True
        admission.recovery_runtime_instance_id = str(RUNTIME_A)
        admission.admission_status = AdmissionStatus.STOP_REQUIRED.value
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
    )

    class Risk:
        calls = []

        async def collect(self, _connection, *, instrument_uid, operations_from, request_ids=()):
            self.calls.append(instrument_uid)
            captured_at = NOW - timedelta(seconds=1)
            return AdmissionRiskSnapshot(
                snapshot_id="00000000-0000-4000-8000-000000000041",
                account_id="synthetic-account",
                capture_started_at=NOW - timedelta(seconds=2),
                captured_at=captured_at,
                valid_until=NOW + timedelta(seconds=20),
                equity=Decimal("100000"),
                equity_currency="RUB",
                money=None,
                money_currency=None,
                blocked_cash=None,
                blocked_cash_currency=None,
                money_loaded=False,
                positions=(),
                ordinary_orders=(),
                stop_orders=(),
                order_states=(),
                operations=(),
                operations_complete=True,
                operations_through=captured_at,
                instrument=None,
            )

    risk = Risk()
    admission_service._risk = risk
    admission_service._connection_resolver = lambda _broker_id: BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="t_invest",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic test credential.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )

    result = asyncio.run(admission_service.prepare_daily(str(BROKER_ID)))

    assert result.reason is None
    assert risk.calls == [None]
    view = admission_service.view(str(BROKER_ID))
    assert view.baseline is not None
    assert view.baseline.moscow_day == NOW.date()
    assert view.admission_status is AdmissionStatus.DISARMED
    assert view.recovery_only is True
    revision = view.revision

    duplicate = asyncio.run(admission_service.prepare_daily(str(BROKER_ID)))

    assert duplicate.reason is None
    assert risk.calls == [None]
    assert admission_service.view(str(BROKER_ID)).revision == revision


def test_daily_preparation_recovers_technical_hold_without_unresolved_reservations(admission_service, core_engine):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with admission_service._repository._factory.begin() as session:
        owner = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        owner.access_mode = "TRADE"
        owner.strategy_enabled = True
        broker = session.get(UserBrokerModel, str(BROKER_ID))
        broker.fqdn = "invest-public-api.tbank.ru:443"
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target=broker.fqdn,
            token="synthetic-token",  # noqa: S106 - deterministic test credential.
        )
        session.get(TradingAccountAdmissionModel, str(BROKER_ID)).configuration_fingerprint = fingerprint
    with admission_service._repository._factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.recovery_only = True
        admission.recovery_runtime_instance_id = str(RUNTIME_A)
        automation = automation_model(
            "cancelled-technical-hold",
            user_broker_id=str(BROKER_ID),
            instrument_id="technical-hold-instrument",
            state="HOLD",
        )
        automation.revision = 2
        automation.suspended_from_state = "IN_WORK"
        automation.hold_reason = "Uncertain broker intent could not be reconciled unambiguously"
        session.add(instrument_model("technical-hold-instrument", str(BROKER_ID)))
        session.flush()
        session.add(automation)
        session.flush()
        session.add(
            AutomationEventModel(
                event_id=str(uuid4()),
                automation_id=automation.id,
                user_broker_id=str(BROKER_ID),
                sequence_number=1,
                expected_revision=1,
                fact_kind="AUTOMATION_STATE_CHANGED",
                safe_message=automation.hold_reason,
                payload={
                    "state": "HOLD",
                    "suspended_from_state": "IN_WORK",
                    "hold_reason": automation.hold_reason,
                },
                occurred_at=NOW,
                received_at=NOW,
            )
        )

    class CompleteRisk:
        async def collect(self, _connection, **_kwargs):
            return AdmissionRiskSnapshot(
                snapshot_id=str(uuid4()),
                account_id="synthetic-account",
                capture_started_at=NOW - timedelta(seconds=2),
                captured_at=NOW - timedelta(seconds=1),
                valid_until=NOW + timedelta(seconds=20),
                equity=Decimal("100000"),
                equity_currency="RUB",
                money=Decimal("100000"),
                money_currency="RUB",
                blocked_cash=Decimal(0),
                blocked_cash_currency="RUB",
                money_loaded=True,
                positions=(),
                ordinary_orders=(),
                stop_orders=(),
                order_states=(),
                operations=(),
                operations_complete=True,
                operations_through=NOW - timedelta(seconds=1),
                instrument=None,
            )

    admission_service._risk = CompleteRisk()
    admission_service._connection_resolver = lambda _broker_id: BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="t_invest",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic test credential.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )

    result = asyncio.run(admission_service.prepare_daily(str(BROKER_ID)))

    assert result.reason is None
    view = admission_service.view(str(BROKER_ID))
    assert view.recovery_only is False
    with admission_service._repository._factory() as session:
        current = session.get(TradingAutomationModel, "cancelled-technical-hold")
        assert current.state == "HOLD"
        assert current.resume_requested is True
        assert current.revision == 2
        assert session.query(TradingSubmitReservationModel).count() == 0


def test_expired_owner_lease_cannot_be_renewed_and_does_not_create_sticky_stop(admission_service, core_engine):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    expired_at = NOW - timedelta(seconds=1)
    with core_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE trading_account_admissions SET lease_expires_at = ? WHERE user_broker_id = ?",
            (expired_at, str(BROKER_ID)),
        )
        connection.exec_driver_sql(
            "UPDATE worker_runtime_instances SET lease_expires_at = ? WHERE runtime_instance_id = ?",
            (expired_at, str(RUNTIME_A)),
        )

    with pytest.raises(ValueError, match="cannot be renewed"):
        admission_service.renew_owner(RUNTIME_A, 1)

    view = admission_service.view(str(BROKER_ID))
    assert view.admission_status is AdmissionStatus.DISARMED
    assert view.lease_expires_at == expired_at


def test_runtime_uuid_cannot_be_reused_with_another_secret(admission_service):
    admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with pytest.raises(ValueError, match="already bound"):
        admission_service.register_runtime(request(RUNTIME_A), SECRET_B)


def test_submit_idempotency_lookup_failure_never_claims_a_definite_no_send(admission_service, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(admission_service._repository, "submit_attempt", unavailable)
    result = asyncio.run(
        admission_service.authorize_submit(
            SubmitAuthorizationRequest(
                broker_id=BROKER_ID,
                account_id="synthetic-account",
                automation_id=UUID("00000000-0000-4000-8000-000000000004"),
                idempotency_key=UUID("00000000-0000-4000-8000-000000000005"),
                generation=1,
                revision=1,
                configuration_fingerprint="a" * 64,
                instrument_uid="canary-uid",
                side="BUY",
                lot_size=10,
                currency="RUB",
                quantity_lots=1,
                limit_price=Decimal("10"),
                market_valid_until=NOW + timedelta(seconds=2),
                submit_deadline=NOW + timedelta(seconds=1),
            ),
            runtime_instance_id=RUNTIME_A,
            generation=1,
        )
    )

    assert isinstance(result, SubmitDenied)
    assert result.reason is SubmitDenialReason.CORE_UNAVAILABLE
    assert result.outcome_uncertain


def test_complete_snapshot_with_no_canary_position_is_a_zero_position():
    request_value = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        automation_id=UUID("00000000-0000-4000-8000-000000000004"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000005"),
        generation=1,
        revision=1,
        configuration_fingerprint="a" * 64,
        instrument_uid="canary-uid",
        side="BUY",
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("10.00"),
        market_valid_until=NOW + timedelta(seconds=2),
        submit_deadline=NOW + timedelta(seconds=1),
    )
    snapshot = AdmissionRiskSnapshot(
        snapshot_id="00000000-0000-4000-8000-000000000006",
        account_id="synthetic-account",
        capture_started_at=NOW - timedelta(milliseconds=100),
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        equity=Decimal("1000"),
        equity_currency="RUB",
        money=Decimal("1000"),
        money_currency="RUB",
        blocked_cash=Decimal("0"),
        blocked_cash_currency="RUB",
        money_loaded=True,
        positions=(),
        ordinary_orders=(),
        stop_orders=(),
        order_states=(),
        operations=(),
        operations_complete=True,
        operations_through=NOW,
        instrument=AdmissionInstrument(
            instrument_uid="canary-uid",
            instrument_type="SHARE",
            currency="RUB",
            lot_size=10,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("10"),
            mark_captured_at=NOW,
        ),
    )
    estimate = AdmissionOrderEstimate(
        initial_order_amount=Decimal("100"),
        total_commission=Decimal("1"),
        currency="RUB",
        captured_at=NOW,
    )
    context = SimpleNamespace(account_id="synthetic-account", lot_size=10, min_price_increment=Decimal("0.01"))

    result = TradingAdmissionService._validate_submit_snapshot(snapshot, context, estimate, request_value, NOW)

    assert result[:3] == (Decimal("100"), Decimal("1"), {})
    assert result[3]["canary-uid"].lot_size == 10
    assert result[3]["canary-uid"].lot_size == 10


@pytest.mark.parametrize(
    ("equity", "unknown_reasons"),
    [(Decimal("NaN"), ()), (Decimal("1000"), ("portfolio unavailable",))],
)
def test_submit_snapshot_rejects_unknown_or_nonfinite_source_data(equity, unknown_reasons):
    snapshot = AdmissionRiskSnapshot(
        snapshot_id="00000000-0000-4000-8000-000000000006",
        account_id="synthetic-account",
        capture_started_at=NOW - timedelta(milliseconds=100),
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=1),
        equity=equity,
        equity_currency="RUB",
        money=Decimal("1000"),
        money_currency="RUB",
        blocked_cash=Decimal("0"),
        blocked_cash_currency="RUB",
        money_loaded=True,
        positions=(),
        ordinary_orders=(),
        stop_orders=(),
        order_states=(),
        operations=(),
        operations_complete=True,
        operations_through=NOW,
        instrument=AdmissionInstrument(
            instrument_uid="canary-uid",
            instrument_type="SHARE",
            currency="RUB",
            lot_size=10,
            price_increment=Decimal("0.01"),
            api_trade_available=True,
            mark_price=Decimal("10"),
            mark_captured_at=NOW,
        ),
        unknown_reasons=unknown_reasons,
    )
    request_value = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        automation_id=UUID("00000000-0000-4000-8000-000000000004"),
        idempotency_key=UUID("00000000-0000-4000-8000-000000000005"),
        generation=1,
        revision=1,
        configuration_fingerprint="a" * 64,
        instrument_uid="canary-uid",
        side="BUY",
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("10.00"),
        market_valid_until=NOW + timedelta(seconds=2),
        submit_deadline=NOW + timedelta(seconds=1),
    )
    estimate = AdmissionOrderEstimate(
        initial_order_amount=Decimal("100"),
        total_commission=Decimal("1"),
        currency="RUB",
        captured_at=NOW,
    )

    result = TradingAdmissionService._validate_submit_snapshot(
        snapshot, SimpleNamespace(account_id="synthetic-account", lot_size=10), estimate, request_value, NOW
    )

    assert result is SubmitDenialReason.DATA_UNKNOWN


def test_partial_sell_replaces_reserved_units_from_cumulative_fills():
    key = str(UUID("00000000-0000-4000-8000-000000000005"))
    reconciliation = SimpleNamespace(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000011"),
        position_anchors=(SimpleNamespace(instrument_uid="canary-uid", quantity_units=Decimal(20), lot_size=10),),
    )
    reservation = AdmissionReservationRecord(
        idempotency_key=key,
        state="IN_FLIGHT",
        side="SELL",
        instrument_uid="canary-uid",
        requested_amount=Decimal("200"),
        reserved_cash=Decimal(0),
        reserved_position_units=Decimal(20),
        quantity_lots=2,
        runtime_instance_id=str(RUNTIME_A),
        generation=1,
        anchor_reconciliation_id=str(reconciliation.reconciliation_id),
        anchor_executed_lots=0,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
    )
    context = SimpleNamespace(
        reconciliation=reconciliation,
        reservations=(reservation,),
        lot_size=10,
        instrument_uid="canary-uid",
    )

    def snapshot(status, executed_lots):
        state = AdmissionOrderState(
            request_id=key,
            broker_order_id="broker-order-1",
            instrument_uid="canary-uid",
            side=OrderSide.SELL,
            requested_lots=2,
            executed_lots=executed_lots,
            executed_amount=Decimal(executed_lots * 100),
            executed_commission=Decimal(executed_lots),
            currency="RUB",
            status=status,
            attributable=True,
            unknown_reason=None,
        )
        orders = (
            ()
            if status == "FILLED"
            else (AdmissionOrder("broker-order-1", "canary-uid", key, status, OrderSide.SELL, 2),)
        )
        return SimpleNamespace(
            order_states=(state,),
            ordinary_orders=orders,
            positions=(
                SimpleNamespace(
                    instrument_uid="canary-uid",
                    quantity_units=Decimal(20 - executed_lots * 10),
                ),
            ),
        )

    partial = TradingAdmissionService._project_submit_reservations(
        context, snapshot("PARTIALLYFILL", 1), {"canary-uid": (Decimal(10), 10)}
    )
    assert isinstance(partial, tuple)
    assert partial[0].reserved_position_units == Decimal(10)
    assert partial[0].executed_lots == 1

    context = SimpleNamespace(
        reconciliation=reconciliation, reservations=partial, lot_size=10, instrument_uid="canary-uid"
    )
    filled = TradingAdmissionService._project_submit_reservations(
        context, snapshot("FILLED", 2), {"canary-uid": (Decimal(0), 10)}
    )
    assert isinstance(filled, tuple)
    assert filled[0].state == "SETTLED"
    assert filled[0].reserved_position_units == 0
    assert filled[0].executed_lots == 2


def test_legacy_scalar_anchor_is_used_only_for_its_uid_with_known_lot_size():
    key = str(UUID("00000000-0000-4000-8000-000000000105"))
    reconciliation = SimpleNamespace(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000111"),
        scope="CANARY_UID",
        status=ReconciliationStatus.CANARY_READY,
        canary_uid="legacy-gold-uid",
        position_anchor_units=Decimal(20),
        position_anchors=(),
    )
    reservation = AdmissionReservationRecord(
        idempotency_key=key,
        state="IN_FLIGHT",
        side="SELL",
        instrument_uid="legacy-gold-uid",
        requested_amount=Decimal("200"),
        reserved_cash=Decimal(0),
        reserved_position_units=Decimal(20),
        quantity_lots=2,
        runtime_instance_id=str(RUNTIME_A),
        generation=1,
        anchor_reconciliation_id=str(reconciliation.reconciliation_id),
        anchor_executed_lots=0,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
    )
    state = AdmissionOrderState(
        request_id=key,
        broker_order_id="legacy-broker-order",
        instrument_uid="legacy-gold-uid",
        side=OrderSide.SELL,
        requested_lots=2,
        executed_lots=1,
        executed_amount=Decimal("100"),
        executed_commission=Decimal(1),
        currency="RUB",
        status="PARTIALLYFILL",
        attributable=True,
        unknown_reason=None,
    )
    context = SimpleNamespace(reconciliation=reconciliation, reservations=(reservation,))
    snapshot = SimpleNamespace(
        order_states=(state,),
        ordinary_orders=(
            AdmissionOrder("legacy-broker-order", "legacy-gold-uid", key, "PARTIALLYFILL", OrderSide.SELL, 2),
        ),
    )

    projected = TradingAdmissionService._project_submit_reservations(
        context,
        snapshot,
        {"legacy-gold-uid": (Decimal(10), Decimal(0))},
        {"legacy-gold-uid": SimpleNamespace(lot_size=10)},
    )
    assert isinstance(projected, tuple)
    assert projected[0].state == "BROKER_ACTIVE"
    assert projected[0].reserved_position_units == Decimal(10)

    no_lot = TradingAdmissionService._project_submit_reservations(
        context,
        snapshot,
        {"legacy-gold-uid": (Decimal(10), Decimal(0))},
        {"legacy-gold-uid": SimpleNamespace(lot_size=None)},
    )
    assert no_lot is SubmitDenialReason.DATA_UNKNOWN

    other_uid_reservation = replace(reservation, instrument_uid="other-uid")
    context.reservations = (reservation, other_uid_reservation)
    multiple_uid = TradingAdmissionService._project_submit_reservations(
        context,
        snapshot,
        {"legacy-gold-uid": (Decimal(10), Decimal(0)), "other-uid": (Decimal(1), Decimal(0))},
        {"legacy-gold-uid": SimpleNamespace(lot_size=10), "other-uid": SimpleNamespace(lot_size=5)},
    )
    assert multiple_uid is SubmitDenialReason.DATA_UNKNOWN

    context.reservations = (reservation,)
    duplicate_state = SimpleNamespace(order_states=(state, state), ordinary_orders=())
    assert (
        TradingAdmissionService._project_submit_reservations(
            context,
            duplicate_state,
            {"legacy-gold-uid": (Decimal(10), Decimal(0))},
            {"legacy-gold-uid": SimpleNamespace(lot_size=10)},
        )
        is SubmitDenialReason.DATA_UNKNOWN
    )


@pytest.mark.parametrize(
    ("anchor_present", "state_count", "state_uid", "expected"),
    [
        (True, 1, "legacy-gold-uid", True),
        (False, 1, "legacy-gold-uid", False),
        (True, 0, "legacy-gold-uid", False),
        (True, 2, "legacy-gold-uid", False),
        (True, 1, "other-uid", False),
    ],
)
def test_repository_reads_legacy_scalar_anchor_only_with_proven_uid_and_broker_state(
    admission_service, core_engine, anchor_present, state_count, state_uid, expected
):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    repository = admission_service._repository
    factory = create_session_factory(core_engine)
    fingerprint = "d" * 64
    reconciliation_id = UUID("00000000-0000-4000-8000-000000000201")
    request_key = UUID("00000000-0000-4000-8000-000000000202")
    legacy = ReconciliationResult(
        reconciliation_id=reconciliation_id,
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="CANARY_UID",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.CANARY_READY,
        canary_uid="legacy-gold-uid",
        captured_at=NOW - timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=30),
        can_arm=True,
        position_anchor_units=Decimal(20),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=1,
        reasons=(),
        risk_snapshot_id=None,
    )
    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.reconciliation_id = str(reconciliation_id)
        admission.configuration_fingerprint = fingerprint
        session.add(
            TradingReconciliationModel(
                reconciliation_id=str(reconciliation_id),
                user_broker_id=str(BROKER_ID),
                account_id="synthetic-account",
                scope="CANARY_UID",
                owner_runtime_instance_id=str(RUNTIME_A),
                owner_generation=owner.generation,
                configuration_fingerprint=fingerprint,
                status="CANARY_READY",
                canary_uid="legacy-gold-uid",
                captured_at=NOW - timedelta(seconds=1),
                valid_until=NOW + timedelta(seconds=30),
                can_arm=True,
                position_anchor_units=Decimal(20),
                active_ordinary_order_count=0,
                active_stop_order_count=0,
                unresolved_reservation_count=1,
                reasons=[],
                risk_snapshot_id=None,
                payload=legacy.model_dump(mode="json"),
            )
        )
        session.add(
            TradingSubmitReservationModel(
                reservation_id=str(UUID("00000000-0000-4000-8000-000000000203")),
                user_broker_id=str(BROKER_ID),
                automation_id=str(UUID("00000000-0000-4000-8000-000000000204")),
                idempotency_key=str(request_key),
                request_fingerprint="e" * 64,
                state="IN_FLIGHT",
                runtime_instance_id=str(RUNTIME_A),
                generation=owner.generation,
                configuration_fingerprint=fingerprint,
                permit_id=None,
                side="SELL",
                instrument_uid="legacy-gold-uid",
                quantity_lots=2,
                limit_price=Decimal(10),
                requested_amount=Decimal(200),
                reserved_cash=Decimal(0),
                reserved_position_units=Decimal(20),
                payload={
                    "anchor_reconciliation_id": str(reconciliation_id),
                    "anchor_executed_lots": 0,
                    "executed_lots": 0,
                    "executed_amount": "0",
                    "executed_commission": "0",
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
    state = AdmissionOrderState(
        request_id=str(request_key),
        broker_order_id="legacy-order",
        instrument_uid=state_uid,
        side=OrderSide.SELL,
        requested_lots=2,
        executed_lots=1,
        executed_amount=Decimal(100),
        executed_commission=Decimal(1),
        currency="RUB",
        status="PARTIALLYFILL",
        attributable=True,
        unknown_reason=None,
    )
    order_states = tuple(state for _ in range(state_count))
    anchors = (
        (ReconciliationPositionAnchor(instrument_uid="legacy-gold-uid", quantity_units=Decimal(10), lot_size=10),)
        if anchor_present
        else ()
    )

    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        result = repository._apply_submit_settlement(
            session,
            str(BROKER_ID),
            admission,
            anchors,
            order_states,
            now=NOW,
        )
        reservation = session.get(TradingSubmitReservationModel, str(UUID("00000000-0000-4000-8000-000000000203")))
        assert result is expected
        if expected:
            assert reservation.state == "BROKER_ACTIVE"
            assert reservation.reserved_position_units == Decimal(10)
        else:
            assert reservation.state == "UNKNOWN"


def test_submit_permit_is_consumed_once_and_reconciles_fill_before_next_request(admission_service, core_engine):
    repository = admission_service._repository
    factory = create_session_factory(core_engine)
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
    )
    canary_uid = "canary-uid"
    automation_id = UUID("00000000-0000-4000-8000-000000000004")
    key = UUID("00000000-0000-4000-8000-000000000005")
    with factory.begin() as session:
        instrument = instrument_model("instrument-1", str(BROKER_ID))
        instrument.external_instrument_id = canary_uid
        session.add(instrument)
        session.flush()
        session.add(automation_model(str(automation_id), user_broker_id=str(BROKER_ID)))

    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    with factory.begin() as session:
        runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        runtime.access_mode = "TRADE"
        runtime.strategy_enabled = True
        session.get(TradingAccountAdmissionModel, str(BROKER_ID)).configuration_fingerprint = fingerprint
    baseline = AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000010"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        moscow_day=NOW.date(),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal(0),
        capture_started_at=NOW - timedelta(seconds=2),
        capture_finished_at=NOW - timedelta(seconds=1),
        source="GET_PORTFOLIO",
        operations_through=NOW - timedelta(seconds=1),
        accepted_by="operator",
        accepted_at=NOW,
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )
    repository.save_baseline(baseline, configuration_fingerprint=fingerprint)
    limits = AdmissionLimits(
        currency="RUB",
        max_order_notional=Decimal("10000"),
        exposure_scope="ACCOUNT_GROSS",
        max_exposure=Decimal("20000"),
        max_outstanding_orders=1,
        max_daily_loss=Decimal("1000"),
    )
    configured = repository.update_limits(
        str(BROKER_ID),
        UpdateLimitsRequest(expected_revision=2, limits=limits),
        actor="operator",
        now=NOW,
    )
    assert configured.limits == limits
    assert configured.baseline is not None
    assert configured.baseline.baseline_id == baseline.baseline_id
    with pytest.raises(AdmissionConflictError, match="revision"):
        repository.update_limits(
            str(BROKER_ID),
            UpdateLimitsRequest(expected_revision=2, limits=limits.model_copy(update={"max_exposure": 1})),
            actor="operator",
            now=NOW,
        )
    submit = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        automation_id=automation_id,
        idempotency_key=key,
        generation=owner.generation,
        revision=configured.revision,
        configuration_fingerprint=fingerprint,
        instrument_uid=canary_uid,
        side="BUY",
        lot_size=10,
        currency="RUB",
        quantity_lots=1,
        limit_price=Decimal("10"),
        market_valid_until=NOW + timedelta(seconds=15),
        submit_deadline=NOW + timedelta(seconds=14),
    )
    refreshed = repository.renew_owner(
        str(RUNTIME_A),
        owner.generation,
        final_clock=lambda: NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    aged_proof_view = repository.get_view(str(BROKER_ID), now=NOW + timedelta(seconds=2))
    assert refreshed.registration_status is RuntimeRegistrationStatus.OWNER
    assert aged_proof_view.revision == configured.revision
    assert aged_proof_view.effective_can_submit

    class FreshRisk:
        def __init__(self):
            self.position_units = Decimal(0)
            self.order_states = {}
            self.hold_automation_on_collect = False

        async def collect(self, connection, *, instrument_uid, operations_from, request_ids=(), instrument_uids=()):
            if self.hold_automation_on_collect:
                with factory.begin() as session:
                    session.get(TradingAutomationModel, str(automation_id)).state = "HOLD"
                self.hold_automation_on_collect = False
            return AdmissionRiskSnapshot(
                snapshot_id="00000000-0000-4000-8000-000000000017",
                account_id="synthetic-account",
                capture_started_at=NOW + timedelta(milliseconds=1500),
                captured_at=NOW + timedelta(milliseconds=1600),
                valid_until=NOW + timedelta(seconds=3),
                equity=Decimal("100000"),
                equity_currency="RUB",
                money=Decimal("1000"),
                money_currency="RUB",
                blocked_cash=Decimal(0),
                blocked_cash_currency="RUB",
                money_loaded=True,
                positions=(
                    ()
                    if not self.position_units
                    else (
                        AdmissionPosition(
                            instrument_uid=canary_uid,
                            quantity_units=self.position_units,
                            blocked_units=Decimal(0),
                            mark_price=Decimal("10"),
                            currency="RUB",
                            lot_size=10,
                            mark_captured_at=NOW + timedelta(milliseconds=1600),
                        ),
                    )
                ),
                ordinary_orders=(),
                stop_orders=(),
                order_states=tuple(self.order_states[key] for key in request_ids if key in self.order_states),
                operations=(),
                operations_complete=True,
                operations_through=NOW + timedelta(milliseconds=1600),
                instrument=AdmissionInstrument(
                    instrument_uid=canary_uid,
                    instrument_type="SHARE",
                    currency="RUB",
                    lot_size=10,
                    price_increment=Decimal("0.01"),
                    api_trade_available=True,
                    mark_price=Decimal("10"),
                    mark_captured_at=NOW + timedelta(milliseconds=1600),
                ),
            )

        async def estimate_limit_order(self, connection, *, instrument_uid, side, quantity_lots, limit_price):
            return AdmissionOrderEstimate(
                initial_order_amount=Decimal("100"),
                total_commission=Decimal("1"),
                currency="RUB",
                captured_at=NOW + timedelta(milliseconds=1600),
            )

    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - never used outside the fake connection.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )
    fresh_risk = FreshRisk()
    admission_service._risk = fresh_risk
    admission_service._connection_resolver = lambda _: connection
    admission_service._clock = lambda: NOW + timedelta(seconds=2)
    permit = asyncio.run(
        admission_service.authorize_submit(submit, runtime_instance_id=RUNTIME_A, generation=owner.generation)
    )

    assert isinstance(permit, SubmitPermit), permit
    fresh_risk.position_units = Decimal(10)
    fresh_risk.order_states[str(key)] = AdmissionOrderState(
        request_id=str(key),
        broker_order_id="broker-order-1",
        instrument_uid=canary_uid,
        side=OrderSide.BUY,
        requested_lots=1,
        executed_lots=1,
        executed_amount=Decimal("100"),
        executed_commission=Decimal("1"),
        currency="RUB",
        status="FILLED",
        attributable=True,
        unknown_reason=None,
    )
    second_key = UUID("00000000-0000-4000-8000-000000000018")
    second_submit = submit.model_copy(update={"idempotency_key": second_key, "side": "SELL"})
    next_permit = asyncio.run(
        admission_service.authorize_submit(second_submit, runtime_instance_id=RUNTIME_A, generation=owner.generation)
    )
    assert isinstance(next_permit, SubmitPermit), next_permit

    fresh_risk.order_states[str(second_key)] = AdmissionOrderState(
        request_id=str(second_key),
        broker_order_id="broker-order-2",
        instrument_uid=canary_uid,
        side=OrderSide.SELL,
        requested_lots=1,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
        currency="RUB",
        status="CANCELLED",
        attributable=True,
        unknown_reason=None,
    )
    fresh_risk.hold_automation_on_collect = True
    third_key = UUID("00000000-0000-4000-8000-000000000019")
    held_result = asyncio.run(
        admission_service.authorize_submit(
            submit.model_copy(update={"idempotency_key": third_key, "side": "SELL"}),
            runtime_instance_id=RUNTIME_A,
            generation=owner.generation,
        )
    )
    assert isinstance(held_result, SubmitDenied)
    assert held_result.reason is SubmitDenialReason.AUTOMATION_NOT_ACTIVE
    with factory() as session:
        assert (
            session.scalar(
                select(TradingSubmitReservationModel.reservation_id).where(
                    TradingSubmitReservationModel.idempotency_key == str(third_key)
                )
            )
            is None
        )
    with factory() as session:
        first_reservation = session.get(TradingSubmitReservationModel, str(permit.permit_id))
    assert first_reservation.state == "SETTLED"
    assert first_reservation.reserved_cash == 0
    revoked = admission_service.revoke(
        str(BROKER_ID),
        RevokeRequest(expected_revision=configured.revision, reason="operator requested stop"),
        actor="operator",
    )
    denied = asyncio.run(
        admission_service.authorize_submit(
            submit.model_copy(update={"limit_price": Decimal("11")}),
            runtime_instance_id=RUNTIME_A,
            generation=owner.generation,
        )
    )
    assert revoked.admission_status is AdmissionStatus.DISARMED
    assert isinstance(denied, SubmitDenied)
    assert denied.reason is SubmitDenialReason.ALREADY_CONSUMED
    assert denied.outcome_uncertain
    assert denied.attempt is not None
    assert not denied.attempt.proven_not_granted

    with pytest.raises(AdmissionConflictError, match="already accepted"):
        repository.save_baseline(
            baseline.model_copy(update={"baseline_id": UUID("00000000-0000-4000-8000-000000000015")}),
            configuration_fingerprint=fingerprint,
        )
    next_day = NOW + timedelta(days=1)
    repository.save_baseline(
        AdmissionBaseline(
            baseline_id=UUID("00000000-0000-4000-8000-000000000016"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            moscow_day=next_day.date(),
            timezone="Europe/Moscow",
            currency="RUB",
            equity=Decimal("100000"),
            net_external_cash_flow=Decimal(0),
            capture_started_at=next_day - timedelta(seconds=2),
            capture_finished_at=next_day - timedelta(seconds=1),
            source="GET_PORTFOLIO",
            operations_through=next_day - timedelta(seconds=1),
            accepted_by="operator",
            accepted_at=next_day,
            policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
        ),
        configuration_fingerprint=fingerprint,
    )
    next_day_view = repository.get_view(str(BROKER_ID), now=NOW)
    assert next_day_view.admission_status is AdmissionStatus.DISARMED
    assert next_day_view.arm_revision is None


@pytest.mark.parametrize(("filled_lots", "filled_units"), [(0, 0), (1, 10)])
def test_closed_uid_anchor_survives_later_account_grants(admission_service, core_engine, filled_lots, filled_units):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    repository = admission_service._repository
    factory = create_session_factory(core_engine)
    automation_ids = {
        uid: UUID(f"00000000-0000-4000-8000-{suffix}")
        for uid, suffix in (("uid-a", "000000000401"), ("uid-b", "000000000402"), ("uid-c", "000000000403"))
    }
    with factory.begin() as session:
        runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        runtime.access_mode = "TRADE"
        runtime.strategy_enabled = True
        for index, uid in enumerate(automation_ids, start=1):
            instrument = instrument_model(f"multi-uid-{index}", str(BROKER_ID))
            instrument.external_instrument_id = uid
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(str(automation_ids[uid]), user_broker_id=str(BROKER_ID), instrument_id=instrument.id)
            )
        broker = session.get(UserBrokerModel, str(BROKER_ID))
        fingerprint = broker_configuration_fingerprint(
            account_id=broker.external_account_id,
            access_mode="TRADE",
            adapter_code=broker.api_slug,
            broker_id=broker.id,
            environment=broker.environment,
            target=broker.fqdn,
            token=str(broker.settings.get("token", "")),
        )
        session.get(TradingAccountAdmissionModel, str(BROKER_ID)).configuration_fingerprint = fingerprint

    baseline = AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000410"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        moscow_day=NOW.date(),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal(0),
        capture_started_at=NOW - timedelta(seconds=2),
        capture_finished_at=NOW - timedelta(seconds=1),
        source="GET_PORTFOLIO",
        operations_through=NOW - timedelta(seconds=1),
        accepted_by="synthetic-operator",
        accepted_at=NOW,
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )
    repository.save_baseline(baseline, configuration_fingerprint=fingerprint)
    configured = repository.update_limits(
        str(BROKER_ID),
        UpdateLimitsRequest(
            expected_revision=repository.get_view(str(BROKER_ID), now=NOW).revision,
            limits=AdmissionLimits(
                currency="RUB",
                max_order_notional=Decimal("10000"),
                max_exposure=Decimal("20000"),
                max_outstanding_orders=10,
                max_daily_loss=Decimal("1000"),
            ),
        ),
        actor="synthetic-operator",
        now=NOW,
    )
    initial_anchor = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000411"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=20),
        can_arm=True,
        position_anchor_units=None,
        position_anchors=tuple(
            ReconciliationPositionAnchor(instrument_uid=uid, quantity_units=0, lot_size=10) for uid in automation_ids
        ),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=None,
    )
    repository.save_reconciliation(
        initial_anchor,
        expected_revision=configured.revision,
        now=NOW,
    )
    revision = repository.get_view(str(BROKER_ID), now=NOW).revision

    class MultiUidRisk:
        def __init__(self):
            self.states = {}
            self.instrument_requests = []
            self.position_units = {}

        async def collect(self, _connection, *, instrument_uid, request_ids=(), instrument_uids=(), **_kwargs):
            self.instrument_requests.append(tuple(instrument_uids))
            captured = NOW + timedelta(milliseconds=500)
            all_uids = sorted(
                set(instrument_uids)
                | {uid for uid, quantity_units in self.position_units.items() if quantity_units > 0}
                | {instrument_uid}
            )
            instruments = tuple(
                AdmissionInstrument(uid, "SHARE", "RUB", 10, Decimal("0.01"), True, Decimal("10"), captured)
                for uid in all_uids
            )
            return AdmissionRiskSnapshot(
                snapshot_id=str(uuid4()),
                account_id="synthetic-account",
                capture_started_at=captured - timedelta(milliseconds=10),
                captured_at=captured,
                valid_until=NOW + timedelta(seconds=2),
                equity=Decimal("100000"),
                equity_currency="RUB",
                money=Decimal("1000"),
                money_currency="RUB",
                blocked_cash=Decimal(0),
                blocked_cash_currency="RUB",
                money_loaded=True,
                positions=tuple(
                    AdmissionPosition(
                        instrument_uid=uid,
                        quantity_units=Decimal(quantity_units),
                        blocked_units=Decimal(0),
                        mark_price=Decimal("10"),
                        currency="RUB",
                        lot_size=10,
                        mark_captured_at=captured,
                    )
                    for uid, quantity_units in self.position_units.items()
                    if quantity_units > 0
                ),
                ordinary_orders=(),
                stop_orders=(),
                order_states=tuple(self.states[key] for key in request_ids if key in self.states),
                operations=(),
                operations_complete=True,
                operations_through=captured,
                instrument=next(item for item in instruments if item.instrument_uid == instrument_uid),
                instruments=instruments,
            )

        async def estimate_limit_order(self, *_args, **_kwargs):
            return AdmissionOrderEstimate(Decimal("100"), Decimal("1"), "RUB", NOW + timedelta(milliseconds=500))

    risk = MultiUidRisk()
    connection = BrokerConnection(
        broker_id=str(BROKER_ID),
        adapter_code="tinkoff",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-test-token",  # noqa: S106 - fake connection is never opened.
        is_test=False,
        access_mode="TRADE",
        account_id="synthetic-account",
        configuration_fingerprint=fingerprint,
    )
    admission_service._risk = risk
    admission_service._connection_resolver = lambda _broker_id: connection
    admission_service._clock = lambda: NOW + timedelta(seconds=1)

    async def submit(uid, key_suffix):
        key = UUID(f"00000000-0000-4000-8000-{key_suffix}")
        return await admission_service.authorize_submit(
            SubmitAuthorizationRequest(
                broker_id=BROKER_ID,
                account_id="synthetic-account",
                automation_id=automation_ids[uid],
                idempotency_key=key,
                generation=owner.generation,
                revision=revision,
                configuration_fingerprint=fingerprint,
                instrument_uid=uid,
                side="BUY",
                lot_size=10,
                currency="RUB",
                quantity_lots=1,
                limit_price=Decimal("10"),
                market_valid_until=NOW + timedelta(seconds=15),
                submit_deadline=NOW + timedelta(seconds=14),
            ),
            runtime_instance_id=RUNTIME_A,
            generation=owner.generation,
        )

    permit_a = asyncio.run(submit("uid-a", "000000000421"))
    risk.states[str(permit_a.idempotency_key)] = AdmissionOrderState(
        request_id=str(permit_a.idempotency_key),
        broker_order_id="broker-a",
        instrument_uid="uid-a",
        side=OrderSide.BUY,
        requested_lots=1,
        executed_lots=filled_lots,
        executed_amount=Decimal("100") if filled_lots else Decimal(0),
        executed_commission=Decimal("1") if filled_lots else Decimal(0),
        currency="RUB",
        status="FILLED" if filled_lots else "CANCELLED",
        attributable=True,
        unknown_reason=None,
    )
    risk.position_units["uid-a"] = filled_units
    permit_b = asyncio.run(submit("uid-b", "000000000422"))
    assert isinstance(permit_b, SubmitPermit)
    with factory() as session:
        promoted = ReconciliationResult.model_validate(
            session.get(
                TradingReconciliationModel, session.get(TradingAccountAdmissionModel, str(BROKER_ID)).reconciliation_id
            ).payload
        )
    assert {anchor.instrument_uid for anchor in promoted.position_anchors} >= {"uid-a", "uid-b"}
    risk.states[str(permit_b.idempotency_key)] = AdmissionOrderState(
        request_id=str(permit_b.idempotency_key),
        broker_order_id="broker-b",
        instrument_uid="uid-b",
        side=OrderSide.BUY,
        requested_lots=1,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
        currency="RUB",
        status="CANCELLED",
        attributable=True,
        unknown_reason=None,
    )

    permit_c = asyncio.run(submit("uid-c", "000000000423"))

    assert isinstance(permit_a, SubmitPermit)
    assert isinstance(permit_c, SubmitPermit), permit_c
    assert risk.instrument_requests[-1] == ("uid-b",)
    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        current = session.get(TradingReconciliationModel, admission.reconciliation_id)
        anchors = ReconciliationResult.model_validate(current.payload).position_anchors
    assert any(anchor.instrument_uid == "uid-a" and anchor.quantity_units == filled_units for anchor in anchors)

    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        settled = session.scalar(
            select(TradingSubmitReservationModel).where(
                TradingSubmitReservationModel.idempotency_key == str(permit_c.idempotency_key)
            )
        )
        terminal_c = AdmissionOrderState(
            request_id=str(permit_c.idempotency_key),
            broker_order_id="broker-c",
            instrument_uid="uid-c",
            side=OrderSide.BUY,
            requested_lots=1,
            executed_lots=0,
            executed_amount=Decimal(0),
            executed_commission=Decimal(0),
            currency="RUB",
            status="CANCELLED",
            attributable=True,
            unknown_reason=None,
        )
        assert repository._apply_submit_settlement(
            session,
            str(BROKER_ID),
            admission,
            (
                ReconciliationPositionAnchor(instrument_uid="uid-a", quantity_units=Decimal(filled_units), lot_size=10),
                ReconciliationPositionAnchor(instrument_uid="uid-b", quantity_units=Decimal(0), lot_size=10),
                ReconciliationPositionAnchor(instrument_uid="uid-c", quantity_units=Decimal(0), lot_size=10),
            ),
            (terminal_c,),
            now=NOW + timedelta(seconds=1),
        )
        assert settled.state == "SETTLED"
        assert settled.reserved_cash == settled.reserved_position_units == 0

    risk.position_units["uid-a"] = 0
    rotation = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000424"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        captured_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=20),
        can_arm=True,
        position_anchor_units=None,
        position_anchors=(),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000425"),
    )
    view = repository.get_view(str(BROKER_ID), now=NOW + timedelta(seconds=1))
    repository.save_reconciliation(rotation, expected_revision=view.revision, now=NOW + timedelta(seconds=1))
    armed = admission_service.arm(
        str(BROKER_ID),
        ArmRequest(
            expected_revision=view.revision + 1,
            runtime_instance_id=RUNTIME_A,
            baseline_id=baseline.baseline_id,
            reconciliation_id=rotation.reconciliation_id,
            limits=configured.limits,
            configuration_fingerprint=fingerprint,
            expires_at=NOW + timedelta(seconds=15),
        ),
        actor="synthetic-operator",
    )
    revision = armed.revision
    risk.instrument_requests.clear()
    post_rotation_permit = asyncio.run(submit("uid-c", "000000000426"))

    assert isinstance(post_rotation_permit, SubmitPermit), post_rotation_permit
    assert risk.instrument_requests[-1] == ()


def test_automatic_reconciliation_rolls_back_when_reservation_settlement_is_unsafe(admission_service, core_engine):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    repository = admission_service._repository
    factory = create_session_factory(core_engine)
    with factory.begin() as session:
        runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A))
        runtime.access_mode = "TRADE"
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake credential.
    )
    baseline = AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000070"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        moscow_day=NOW.date(),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal(0),
        capture_started_at=NOW - timedelta(seconds=2),
        capture_finished_at=NOW - timedelta(seconds=1),
        source="GET_PORTFOLIO",
        operations_through=NOW - timedelta(seconds=1),
        accepted_by="operator",
        accepted_at=NOW,
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )
    repository.save_baseline(baseline, configuration_fingerprint=fingerprint)
    with factory.begin() as session:
        session.add(
            TradingSubmitReservationModel(
                reservation_id="00000000-0000-4000-8000-000000000071",
                user_broker_id=str(BROKER_ID),
                automation_id="00000000-0000-4000-8000-000000000072",
                idempotency_key="00000000-0000-4000-8000-000000000073",
                request_fingerprint="c" * 64,
                state="IN_FLIGHT",
                runtime_instance_id=str(RUNTIME_A),
                generation=owner.generation,
                configuration_fingerprint=fingerprint,
                permit_id=None,
                side="BUY",
                instrument_uid="safe-canary",
                quantity_lots=1,
                limit_price=Decimal("100"),
                requested_amount=Decimal("100"),
                reserved_cash=Decimal("101"),
                reserved_position_units=Decimal(0),
                payload={"original_reserved_cash": "101"},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    initial_revision = admission_service.view(str(BROKER_ID)).revision
    reconciliation = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000074"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="CANARY_UID",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.CANARY_READY,
        canary_uid="safe-canary",
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=30),
        can_arm=True,
        position_anchor_units=Decimal(0),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000075"),
    )

    for _ in range(2):
        with pytest.raises(AdmissionConflictError, match="reservation settlement"):
            repository.save_reconciliation(
                reconciliation,
                expected_revision=initial_revision,
                now=NOW,
                require_unarmed=True,
                final_clock=lambda: NOW,
                risk_snapshot_valid_until=NOW + timedelta(seconds=10),
            )
    with pytest.raises(AdmissionConflictError, match="post-lock final clock"):
        repository.save_reconciliation(
            reconciliation,
            expected_revision=initial_revision,
            now=NOW,
            require_unarmed=True,
            risk_snapshot_valid_until=NOW + timedelta(seconds=10),
        )

    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        reservation = session.get(TradingSubmitReservationModel, "00000000-0000-4000-8000-000000000071")
        reconciliations = list(session.scalars(select(TradingReconciliationModel)))
        events = list(session.scalars(select(TradingAdmissionEventModel)))
        assert admission.revision == initial_revision
        assert admission.reconciliation_id is None
        assert admission.recovery_only is False
        assert reservation.state == "IN_FLIGHT"
        assert reconciliations == []
        assert [event.event_type for event in events] == ["RUNTIME_REGISTERED", "BASELINE_ACCEPTED"]


def test_account_reconciliation_with_unresolved_reservations_is_rejected_without_mutation(
    admission_service, core_engine
):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    factory = create_session_factory(core_engine)
    fingerprint = broker_configuration_fingerprint(
        account_id="synthetic-account",
        access_mode="TRADE",
        adapter_code="t_invest",
        broker_id=str(BROKER_ID),
        environment="PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic fake fingerprint input.
    )
    with factory.begin() as session:
        session.get(WorkerRuntimeInstanceModel, str(RUNTIME_A)).access_mode = "TRADE"
        session.add(
            TradingSubmitReservationModel(
                reservation_id="00000000-0000-4000-8000-000000000301",
                user_broker_id=str(BROKER_ID),
                automation_id="00000000-0000-4000-8000-000000000302",
                idempotency_key="00000000-0000-4000-8000-000000000303",
                request_fingerprint="e" * 64,
                state="UNKNOWN",
                runtime_instance_id=str(RUNTIME_A),
                generation=owner.generation,
                configuration_fingerprint=fingerprint,
                permit_id=None,
                side="BUY",
                instrument_uid="uid-a",
                quantity_lots=1,
                limit_price=Decimal("10"),
                requested_amount=Decimal("100"),
                reserved_cash=Decimal("101"),
                reserved_position_units=Decimal(0),
                payload={"original_reserved_cash": "101"},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    revision = admission_service.view(str(BROKER_ID)).revision
    account_result = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000304"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=30),
        can_arm=True,
        position_anchor_units=None,
        position_anchors=(ReconciliationPositionAnchor(instrument_uid="uid-a", quantity_units=0, lot_size=10),),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=1,
        reasons=(),
        risk_snapshot_id=None,
    )

    with pytest.raises(AdmissionConflictError, match="unresolved account reservations"):
        admission_service._repository.save_reconciliation(
            account_result,
            expected_revision=revision,
            now=NOW,
            order_states=(),
        )

    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        reservation = session.get(TradingSubmitReservationModel, "00000000-0000-4000-8000-000000000301")
        assert admission.revision == revision
        assert admission.reconciliation_id is None
        assert reservation.state == "UNKNOWN"
        assert session.get(TradingReconciliationModel, "00000000-0000-4000-8000-000000000304") is None


def _seed_account_anchor(factory, *, reconciliation_id, owner, fingerprint, positions):
    result = ReconciliationResult(
        reconciliation_id=reconciliation_id,
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW + timedelta(seconds=30),
        can_arm=True,
        position_anchor_units=None,
        position_anchors=positions,
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=None,
    )
    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        admission.reconciliation_id = str(reconciliation_id)
        admission.configuration_fingerprint = fingerprint
        session.add(
            TradingReconciliationModel(
                reconciliation_id=str(reconciliation_id),
                user_broker_id=str(BROKER_ID),
                account_id="synthetic-account",
                scope="ACCOUNT",
                owner_runtime_instance_id=str(RUNTIME_A),
                owner_generation=owner.generation,
                configuration_fingerprint=fingerprint,
                status=ReconciliationStatus.ACCOUNT_ONLY.value,
                canary_uid=None,
                captured_at=NOW,
                valid_until=NOW + timedelta(seconds=30),
                can_arm=True,
                position_anchor_units=None,
                active_ordinary_order_count=0,
                active_stop_order_count=0,
                unresolved_reservation_count=0,
                reasons=[],
                risk_snapshot_id=None,
                payload=result.model_dump(mode="json"),
            )
        )


@pytest.mark.parametrize(("executed_lots", "expected"), [(1, True), (2, False)])
def test_submit_settlement_ignores_settled_closed_uid_already_in_account_anchor(
    admission_service, core_engine, executed_lots, expected
):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    factory = create_session_factory(core_engine)
    fingerprint = "f" * 64
    reconciliation_id = UUID("00000000-0000-4000-8000-000000000401")
    _seed_account_anchor(
        factory,
        reconciliation_id=reconciliation_id,
        owner=owner,
        fingerprint=fingerprint,
        positions=(ReconciliationPositionAnchor(instrument_uid="uid-b", quantity_units=Decimal(10), lot_size=10),),
    )
    reservation_id = "00000000-0000-4000-8000-000000000402"
    key = "00000000-0000-4000-8000-000000000403"
    with factory.begin() as session:
        session.add(
            TradingSubmitReservationModel(
                reservation_id=reservation_id,
                user_broker_id=str(BROKER_ID),
                automation_id="00000000-0000-4000-8000-000000000404",
                idempotency_key=key,
                request_fingerprint="a" * 64,
                state="SETTLED",
                runtime_instance_id=str(RUNTIME_A),
                generation=owner.generation,
                configuration_fingerprint=fingerprint,
                permit_id=None,
                side="BUY",
                instrument_uid="uid-a",
                quantity_lots=1,
                limit_price=Decimal(10),
                requested_amount=Decimal(100),
                reserved_cash=Decimal(0),
                reserved_position_units=Decimal(0),
                payload={
                    "anchor_reconciliation_id": str(reconciliation_id),
                    "anchor_executed_lots": executed_lots,
                    "executed_lots": executed_lots,
                    "executed_amount": "100",
                    "executed_commission": "1",
                    "last_reconciled_order_state": "FILLED",
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )

    repository = admission_service._repository
    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        settled_before = session.get(TradingSubmitReservationModel, reservation_id)
        facts_before = dict(settled_before.payload)
        result = repository._apply_submit_settlement(
            session,
            str(BROKER_ID),
            admission,
            (ReconciliationPositionAnchor(instrument_uid="uid-b", quantity_units=Decimal(10), lot_size=10),),
            (),
            now=NOW,
        )
        assert result is expected
        assert settled_before.state == "SETTLED"
        assert settled_before.reserved_cash == settled_before.reserved_position_units == 0
        assert settled_before.payload == facts_before


def test_submit_settlement_rejects_unresolved_reservation_from_stale_account_anchor(admission_service, core_engine):
    owner = admission_service.register_runtime(request(RUNTIME_A), SECRET_A)
    factory = create_session_factory(core_engine)
    fingerprint = "f" * 64
    reconciliation_id = UUID("00000000-0000-4000-8000-000000000411")
    old_anchor_id = "00000000-0000-4000-8000-000000000412"
    _seed_account_anchor(
        factory,
        reconciliation_id=reconciliation_id,
        owner=owner,
        fingerprint=fingerprint,
        positions=(ReconciliationPositionAnchor(instrument_uid="uid-b", quantity_units=Decimal(10), lot_size=10),),
    )
    reservation_id = "00000000-0000-4000-8000-000000000413"
    key = "00000000-0000-4000-8000-000000000414"
    with factory.begin() as session:
        session.add(
            TradingSubmitReservationModel(
                reservation_id=reservation_id,
                user_broker_id=str(BROKER_ID),
                automation_id="00000000-0000-4000-8000-000000000415",
                idempotency_key=key,
                request_fingerprint="b" * 64,
                state="IN_FLIGHT",
                runtime_instance_id=str(RUNTIME_A),
                generation=owner.generation,
                configuration_fingerprint=fingerprint,
                permit_id=None,
                side="BUY",
                instrument_uid="uid-b",
                quantity_lots=1,
                limit_price=Decimal(10),
                requested_amount=Decimal(100),
                reserved_cash=Decimal(101),
                reserved_position_units=Decimal(0),
                payload={
                    "anchor_reconciliation_id": old_anchor_id,
                    "anchor_executed_lots": 0,
                    "executed_lots": 0,
                    "executed_amount": "0",
                    "executed_commission": "0",
                    "original_reserved_cash": "101",
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )

    repository = admission_service._repository
    state = AdmissionOrderState(
        request_id=key,
        broker_order_id="stale-anchor-order",
        instrument_uid="uid-b",
        side=OrderSide.BUY,
        requested_lots=1,
        executed_lots=0,
        executed_amount=Decimal(0),
        executed_commission=Decimal(0),
        currency="RUB",
        status="NEW",
        attributable=True,
        unknown_reason=None,
    )
    with factory.begin() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        reservation = session.get(TradingSubmitReservationModel, reservation_id)
        result = repository._apply_submit_settlement(
            session,
            str(BROKER_ID),
            admission,
            (ReconciliationPositionAnchor(instrument_uid="uid-b", quantity_units=Decimal(10), lot_size=10),),
            (state,),
            now=NOW,
        )
        assert not result
        assert reservation.state == "UNKNOWN"
        assert reservation.reserved_cash == Decimal(101)
        assert reservation.idempotency_key == key
        assert reservation.payload["anchor_reconciliation_id"] == old_anchor_id
