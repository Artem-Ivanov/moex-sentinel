"""PostgreSQL serialization keeps automatic daily baseline acceptance idempotent."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier, Event
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from moex_sentinel.domain.trading_admission import AdmissionConflictError, broker_configuration_fingerprint
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import UserBrokerModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionBaselineModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.repositories.trading_admission import TradingAdmissionRepository
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    ReconciliationResult,
    ReconciliationStatus,
    RuntimeRegistrationRequest,
)
from tests.storage.trading_facts_helpers import user_broker_model

pytestmark = pytest.mark.postgresql

BROKER_ID = UUID("00000000-0000-4000-8000-000000000151")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000152")


def _now() -> datetime:
    value = datetime.now(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def test_parallel_daily_baseline_acceptance_returns_one_immutable_winner(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-daily-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=_now, owner_lease_seconds=600)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-daily-preparation",
                runtime_instance_id=RUNTIME_ID,
                broker_id=BROKER_ID,
                application_environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
            ),
            "a" * 64,
        )
        with factory.begin() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID))
            runtime.access_mode = "TRADE"
            runtime.lease_expires_at = _now() + timedelta(days=2)
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            admission.lease_expires_at = _now() + timedelta(days=2)
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-daily-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic test-only credential.
        )
        now = _now()
        day = now.astimezone(ZoneInfo("Europe/Moscow")).date()

        def baseline(equity: str) -> AdmissionBaseline:
            return AdmissionBaseline(
                baseline_id=uuid4(),
                broker_id=BROKER_ID,
                account_id="synthetic-daily-account",
                moscow_day=day,
                timezone="Europe/Moscow",
                currency="RUB",
                equity=Decimal(equity),
                net_external_cash_flow=Decimal(0),
                capture_started_at=now - timedelta(seconds=2),
                capture_finished_at=now - timedelta(seconds=1),
                source="GET_PORTFOLIO",
                operations_through=now - timedelta(seconds=1),
                accepted_by="daily-preparation",
                accepted_at=now,
                policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
            )

        candidates = (baseline("100000"), baseline("200000"))
        barrier = Barrier(2)

        def accept(candidate):
            barrier.wait(timeout=5)
            return repository.ensure_daily_baseline(
                candidate,
                configuration_fingerprint=fingerprint,
                expected_owner_runtime_instance_id=str(RUNTIME_ID),
                expected_generation=owner.generation,
                snapshot_valid_until=now + timedelta(minutes=5),
                final_clock=_now,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, candidates))

        assert results[0] == results[1]
        with factory() as session:
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            accepted_events = list(
                session.scalars(
                    select(TradingAdmissionEventModel).where(
                        TradingAdmissionEventModel.user_broker_id == str(BROKER_ID),
                        TradingAdmissionEventModel.event_type == "BASELINE_ACCEPTED",
                    )
                )
            )
            baselines = list(
                session.scalars(
                    select(TradingAdmissionBaselineModel).where(
                        TradingAdmissionBaselineModel.user_broker_id == str(BROKER_ID),
                        TradingAdmissionBaselineModel.moscow_day == day,
                    )
                )
            )
            assert admission.baseline_id == str(results[0].baseline_id)
            assert len(baselines) == 1
            assert len(accepted_events) == 1
            assert admission.revision == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("final_state", ["next_moscow_day", "expired_snapshot", "owner_changed", "config_changed"])
def test_daily_baseline_rechecks_clock_and_scope_after_waiting_for_broker_lock(
    isolated_postgresql_database_url, final_state
):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-daily-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=_now, owner_lease_seconds=600)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-daily-preparation",
                runtime_instance_id=RUNTIME_ID,
                broker_id=BROKER_ID,
                application_environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
            ),
            "a" * 64,
        )
        with factory.begin() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID))
            runtime.access_mode = "TRADE"
            runtime.lease_expires_at = _now() + timedelta(days=2)
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            admission.lease_expires_at = _now() + timedelta(days=2)
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-daily-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic test-only credential.
        )
        if final_state == "next_moscow_day":
            current_day = _now().astimezone(ZoneInfo("Europe/Moscow")).date()
            captured_at = datetime.combine(current_day, time(23, 59, 59, 500000), ZoneInfo("Europe/Moscow")).astimezone(
                UTC
            )
        else:
            captured_at = _now()
        day = captured_at.astimezone(ZoneInfo("Europe/Moscow")).date()
        baseline = AdmissionBaseline(
            baseline_id=uuid4(),
            broker_id=BROKER_ID,
            account_id="synthetic-daily-account",
            moscow_day=day,
            timezone="Europe/Moscow",
            currency="RUB",
            equity=Decimal("100000"),
            net_external_cash_flow=Decimal(0),
            capture_started_at=captured_at - timedelta(milliseconds=200),
            capture_finished_at=captured_at - timedelta(milliseconds=100),
            source="GET_PORTFOLIO",
            operations_through=captured_at - timedelta(milliseconds=100),
            accepted_by="daily-preparation",
            accepted_at=captured_at,
            policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
        )
        final_now = captured_at
        final_clock_called = Event()

        def final_clock():
            final_clock_called.set()
            return final_now

        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin() as session:
                session.scalar(select(UserBrokerModel).where(UserBrokerModel.id == str(BROKER_ID)).with_for_update())
                future = pool.submit(
                    repository.ensure_daily_baseline,
                    baseline,
                    configuration_fingerprint=fingerprint,
                    expected_owner_runtime_instance_id=str(RUNTIME_ID),
                    expected_generation=owner.generation,
                    snapshot_valid_until=captured_at + timedelta(seconds=1),
                    final_clock=final_clock,
                )
                # The contender is blocked on the broker row and must not sample its final clock yet.
                assert not final_clock_called.wait(timeout=0.2)
                if final_state == "next_moscow_day":
                    final_now = captured_at + timedelta(milliseconds=600)
                elif final_state == "expired_snapshot":
                    final_now = captured_at + timedelta(seconds=2)
                elif final_state == "owner_changed":
                    admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
                    admission.owner_generation += 1
                elif final_state == "config_changed":
                    broker = session.get(UserBrokerModel, str(BROKER_ID))
                    broker.fqdn = "changed.example:443"

            with pytest.raises(AdmissionConflictError, match="Daily baseline"):
                future.result(timeout=5)

        with factory() as session:
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            baselines = list(
                session.scalars(
                    select(TradingAdmissionBaselineModel).where(
                        TradingAdmissionBaselineModel.user_broker_id == str(BROKER_ID)
                    )
                )
            )
            accepted_events = list(
                session.scalars(
                    select(TradingAdmissionEventModel).where(
                        TradingAdmissionEventModel.user_broker_id == str(BROKER_ID),
                        TradingAdmissionEventModel.event_type == "BASELINE_ACCEPTED",
                    )
                )
            )
            assert final_clock_called.is_set()
            assert baselines == []
            assert accepted_events == []
            assert admission.baseline_id is None
            assert admission.admission_status == "DISARMED"
            assert admission.arm_revision is None
            assert admission.arm_expires_at is None
            assert admission.limits is None
            assert admission.revision == 1
    finally:
        engine.dispose()


def test_automatic_reconciliation_rejects_expired_risk_snapshot_after_lock_wait(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-daily-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=_now, owner_lease_seconds=600)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-daily-preparation",
                runtime_instance_id=RUNTIME_ID,
                broker_id=BROKER_ID,
                application_environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
            ),
            "a" * 64,
        )
        with factory.begin() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID))
            runtime.access_mode = "TRADE"
            runtime.lease_expires_at = _now() + timedelta(days=2)
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            admission.lease_expires_at = _now() + timedelta(days=2)
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-daily-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic test-only credential.
        )
        captured_at = _now()
        day = captured_at.astimezone(ZoneInfo("Europe/Moscow")).date()
        baseline = AdmissionBaseline(
            baseline_id=uuid4(),
            broker_id=BROKER_ID,
            account_id="synthetic-daily-account",
            moscow_day=day,
            timezone="Europe/Moscow",
            currency="RUB",
            equity=Decimal("100000"),
            net_external_cash_flow=Decimal(0),
            capture_started_at=captured_at - timedelta(milliseconds=200),
            capture_finished_at=captured_at - timedelta(milliseconds=100),
            source="GET_PORTFOLIO",
            operations_through=captured_at - timedelta(milliseconds=100),
            accepted_by="operator",
            accepted_at=captured_at,
            policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
        )
        repository.save_baseline(baseline, configuration_fingerprint=fingerprint)
        view = repository.get_view(str(BROKER_ID), now=captured_at)
        reconciliation = ReconciliationResult(
            reconciliation_id=uuid4(),
            broker_id=BROKER_ID,
            account_id="synthetic-daily-account",
            scope="CANARY_UID",
            owner_runtime_instance_id=RUNTIME_ID,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.CANARY_READY,
            canary_uid="safe-canary",
            captured_at=captured_at,
            valid_until=captured_at + timedelta(seconds=30),
            can_arm=True,
            position_anchor_units=Decimal(0),
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=0,
            reasons=(),
            risk_snapshot_id=uuid4(),
        )
        final_now = captured_at
        final_clock_called = Event()

        def final_clock():
            final_clock_called.set()
            return final_now

        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin() as session:
                session.scalar(select(UserBrokerModel).where(UserBrokerModel.id == str(BROKER_ID)).with_for_update())
                future = pool.submit(
                    repository.save_reconciliation,
                    reconciliation,
                    expected_revision=view.revision,
                    now=captured_at,
                    require_unarmed=True,
                    final_clock=final_clock,
                    risk_snapshot_valid_until=captured_at + timedelta(seconds=1),
                )
                assert not final_clock_called.wait(timeout=0.2)
                final_now = captured_at + timedelta(seconds=2)

            with pytest.raises(AdmissionConflictError, match="risk snapshot expired"):
                future.result(timeout=5)

        with factory() as session:
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            reconciliations = list(
                session.scalars(
                    select(TradingReconciliationModel).where(
                        TradingReconciliationModel.user_broker_id == str(BROKER_ID)
                    )
                )
            )
            reconcile_events = list(
                session.scalars(
                    select(TradingAdmissionEventModel).where(
                        TradingAdmissionEventModel.user_broker_id == str(BROKER_ID),
                        TradingAdmissionEventModel.event_type == "RECONCILED",
                    )
                )
            )
            assert final_clock_called.is_set()
            assert admission.revision == view.revision
            assert admission.admission_status == "DISARMED"
            assert admission.reconciliation_id is None
            assert reconciliations == []
            assert reconcile_events == []
    finally:
        engine.dispose()
