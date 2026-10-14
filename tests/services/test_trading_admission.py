import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

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
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
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
    ReconciliationResult,
    ReconciliationStatus,
    RevokeRequest,
    RuntimeRegistrationRequest,
    RuntimeRegistrationStatus,
    SubmitAuthorizationRequest,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model

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

    with pytest.raises(ValueError, match="automation instrument"):
        admission_service._repository.get_submit_context(
            str(BROKER_ID), "00000000-0000-4000-8000-000000000004", "canary-uid"
        )


def test_unresolved_reservations_are_checked_by_fresh_submit_settlement_not_view_gate():
    admission = SimpleNamespace(
        admission_status="ARMED",
        owner_generation=1,
        recovery_only=False,
        lease_expires_at=NOW + timedelta(seconds=30),
        arm_expires_at=NOW + timedelta(seconds=60),
    )
    owner = SimpleNamespace(registration_status="OWNER", generation=1)
    baseline = SimpleNamespace(moscow_day=NOW.date())
    reconciliation = SimpleNamespace(can_arm=True, valid_until=NOW + timedelta(seconds=30))

    reasons = TradingAdmissionRepository._effective_reasons(
        admission, owner, baseline, reconciliation, NOW, unresolved=1
    )

    assert SubmitDenialReason.DATA_UNKNOWN not in reasons


def test_owner_stop_assigns_recovery_only_generation_until_fresh_canary_reconciliation(admission_service):
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
    assert stopped.admission_status is AdmissionStatus.STOP_REQUIRED

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
    assert not recovered.recovery_only
    assert recovered.admission_status is AdmissionStatus.DISARMED


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
        assert admission.admission_status == AdmissionStatus.STOP_REQUIRED.value
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

    result = asyncio.run(admission_service.prepare_daily(str(BROKER_ID), canary_uid="safe-canary"))

    assert result.reason.value == "RECOVERY_REQUIRED"
    assert risk.calls == [None]
    view = admission_service.view(str(BROKER_ID))
    assert view.baseline is not None
    assert view.baseline.moscow_day == NOW.date()
    assert view.admission_status is AdmissionStatus.STOP_REQUIRED
    assert view.recovery_only is True
    revision = view.revision

    duplicate = asyncio.run(admission_service.prepare_daily(str(BROKER_ID), canary_uid="safe-canary"))

    assert duplicate.reason.value == "RECOVERY_REQUIRED"
    assert risk.calls == [None]
    assert admission_service.view(str(BROKER_ID)).revision == revision


def test_expired_owner_lease_cannot_be_renewed(admission_service, core_engine):
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
    assert view.admission_status is AdmissionStatus.STOP_REQUIRED
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

    assert result == (Decimal("100"), Decimal("1"), Decimal(0), Decimal(0), Decimal("10"))


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
        position_anchor_units=Decimal(20),
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
    context = SimpleNamespace(reconciliation=reconciliation, reservations=(reservation,), lot_size=10)

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
        return SimpleNamespace(order_states=(state,), ordinary_orders=orders)

    partial = TradingAdmissionService._project_submit_reservations(context, snapshot("PARTIALLYFILL", 1), Decimal(10))
    assert isinstance(partial, tuple)
    assert partial[0].reserved_position_units == Decimal(10)
    assert partial[0].executed_lots == 1

    context = SimpleNamespace(reconciliation=reconciliation, reservations=partial, lot_size=10)
    filled = TradingAdmissionService._project_submit_reservations(context, snapshot("FILLED", 2), Decimal(0))
    assert isinstance(filled, tuple)
    assert filled[0].state == "SETTLED"
    assert filled[0].reserved_position_units == 0
    assert filled[0].executed_lots == 2


@pytest.mark.parametrize(
    ("position_anchor", "expected_state", "expected_can_arm"),
    [(Decimal("10"), "SETTLED", True), (Decimal("9"), "UNKNOWN", False)],
)
def test_submit_permit_is_consumed_once_and_release_requires_attributed_position_anchor(
    admission_service, core_engine, position_anchor, expected_state, expected_can_arm
):
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
    reconciliation = ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000011"),
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        scope="CANARY_UID",
        owner_runtime_instance_id=RUNTIME_A,
        owner_generation=owner.generation,
        configuration_fingerprint=fingerprint,
        status=ReconciliationStatus.CANARY_READY,
        canary_uid=canary_uid,
        captured_at=NOW - timedelta(milliseconds=500),
        valid_until=NOW + timedelta(seconds=1),
        can_arm=True,
        position_anchor_units=Decimal(0),
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(),
        risk_snapshot_id=None,
    )
    repository.save_reconciliation(reconciliation, expected_revision=2, now=NOW)
    limits = AdmissionLimits(
        canary_uid=canary_uid,
        currency="RUB",
        max_order_notional=Decimal("10000"),
        exposure_scope="CANARY_UID_GROSS",
        max_exposure=Decimal("20000"),
        max_outstanding_orders=1,
        max_daily_loss=Decimal("1000"),
    )
    arm_request = ArmRequest(
        expected_revision=3,
        runtime_instance_id=RUNTIME_A,
        baseline_id=baseline.baseline_id,
        reconciliation_id=reconciliation.reconciliation_id,
        limits=limits,
        configuration_fingerprint=fingerprint,
        expires_at=NOW + timedelta(seconds=60),
    )
    with pytest.raises(AdmissionConflictError, match="prerequisites"):
        repository.arm(
            broker_id=str(BROKER_ID),
            request=arm_request,
            now=NOW + timedelta(seconds=2),
            actor="operator",
        )
    with pytest.raises(AdmissionConflictError, match="prerequisites"):
        repository.arm(
            broker_id=str(BROKER_ID),
            request=arm_request.model_copy(update={"expires_at": NOW + timedelta(hours=10)}),
            now=NOW,
            actor="operator",
        )
    armed = repository.arm(
        broker_id=str(BROKER_ID),
        request=arm_request,
        now=NOW,
        actor="operator",
    )
    submit = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id="synthetic-account",
        automation_id=automation_id,
        idempotency_key=key,
        generation=owner.generation,
        revision=armed.revision,
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
    assert aged_proof_view.revision == armed.revision
    assert aged_proof_view.effective_can_submit

    class FreshRisk:
        def __init__(self):
            self.position_units = Decimal(0)
            self.order_states = {}

        async def collect(self, connection, *, instrument_uid, operations_from, request_ids=()):
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
    with factory() as session:
        first_reservation = session.get(TradingSubmitReservationModel, str(permit.permit_id))
    assert first_reservation.state == "SETTLED"
    assert first_reservation.reserved_cash == 0
    revoked = admission_service.revoke(
        str(BROKER_ID),
        RevokeRequest(expected_revision=armed.revision, reason="operator requested stop"),
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

    failed = repository.save_reconciliation(
        ReconciliationResult(
            reconciliation_id=UUID("00000000-0000-4000-8000-000000000012"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            scope="CANARY_UID",
            owner_runtime_instance_id=RUNTIME_A,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.DATA_UNKNOWN,
            canary_uid=canary_uid,
            captured_at=NOW + timedelta(seconds=3),
            valid_until=NOW + timedelta(seconds=33),
            can_arm=False,
            position_anchor_units=None,
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=1,
            reasons=(SubmitDenialReason.DATA_UNKNOWN,),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000015"),
        ),
        expected_revision=revoked.revision,
        now=NOW + timedelta(seconds=3),
    )
    after_failed_attempt = repository.get_view(str(BROKER_ID), now=NOW + timedelta(seconds=3))
    assert failed.status is ReconciliationStatus.DATA_UNKNOWN
    assert after_failed_attempt.reconciliation.reconciliation_id == reconciliation.reconciliation_id
    assert after_failed_attempt.recovery_only

    settled = repository.save_reconciliation(
        ReconciliationResult(
            reconciliation_id=UUID("00000000-0000-4000-8000-000000000013"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            scope="CANARY_UID",
            owner_runtime_instance_id=RUNTIME_A,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.CANARY_READY,
            canary_uid=canary_uid,
            captured_at=NOW + timedelta(seconds=4),
            valid_until=NOW + timedelta(seconds=34),
            can_arm=True,
            position_anchor_units=position_anchor,
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=1,
            reasons=(),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000014"),
        ),
        expected_revision=after_failed_attempt.revision,
        now=NOW + timedelta(seconds=4),
        order_states=(
            AdmissionOrderState(
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
            ),
        ),
    )

    assert settled.can_arm is expected_can_arm
    assert settled.unresolved_reservation_count == (0 if expected_state == "SETTLED" else 1)
    if expected_state == "UNKNOWN":
        assert settled.status is ReconciliationStatus.DATA_UNKNOWN
    with factory() as session:
        reservation = session.get(TradingSubmitReservationModel, str(next_permit.permit_id))
    assert reservation.state == expected_state
    assert reservation.reserved_cash == 0
    assert reservation.reserved_position_units == (0 if expected_state == "SETTLED" else 10)

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
