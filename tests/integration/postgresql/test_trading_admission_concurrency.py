"""PostgreSQL submit consumption serializes snapshots after all relevant row locks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event, Thread
from time import sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from moex_sentinel.domain.trading_admission import broker_configuration_fingerprint
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOrderEstimate,
    AdmissionOrderState,
    AdmissionRiskSnapshot,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionEventModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.trading_admission import AdmissionConflictError, TradingAdmissionRepository
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionLimits,
    ArmRequest,
    ReconciliationResult,
    ReconciliationStatus,
    RuntimeRegistrationRequest,
    SubmitAuthorizationRequest,
    SubmitDenied,
    SubmitPermit,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")


@pytest.mark.postgresql
def test_parallel_owner_renewals_append_only_one_due_checkpoint(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-account")
            broker.environment = "PROD"
            session.add(broker)
        service = TradingAdmissionService(
            TradingAdmissionRepository(factory), clock=lambda: NOW, owner_lease_seconds=600
        )
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-prod-lease-audit",
                runtime_instance_id=RUNTIME_ID,
                broker_id=BROKER_ID,
                application_environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
            ),
            "a" * 64,
        )
        service.renew_owner(RUNTIME_ID, owner.generation)
        barrier = Barrier(2)
        clock_calls = []

        def clock():
            clock_calls.append(NOW + timedelta(seconds=300))
            return NOW + timedelta(seconds=300)

        def renew():
            competing_service = TradingAdmissionService(
                TradingAdmissionRepository(factory), clock=clock, owner_lease_seconds=600
            )
            barrier.wait(timeout=5)
            return competing_service.renew_owner(RUNTIME_ID, owner.generation)

        holder = factory()
        holder.begin()
        holder.scalar(select(UserBrokerModel).where(UserBrokerModel.id == str(BROKER_ID)).with_for_update())
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(renew) for _ in range(2)]
                try:
                    sleep(0.2)
                    assert clock_calls == []
                finally:
                    holder.rollback()
                    holder.close()
                results = [future.result(timeout=5) for future in futures]
        finally:
            holder.close()
        assert all(result.lease_expires_at == NOW + timedelta(seconds=900) for result in results)
        assert clock_calls == [NOW + timedelta(seconds=300)] * 2
        with factory() as session:
            events = list(
                session.scalars(
                    select(TradingAdmissionEventModel)
                    .where(TradingAdmissionEventModel.event_type == "OWNER_LEASE_RENEWED")
                    .order_by(TradingAdmissionEventModel.occurred_at)
                )
            )
            assert [event.occurred_at for event in events] == [NOW, NOW + timedelta(seconds=300)]
            assert [(event.runtime_instance_id, event.generation) for event in events] == [(str(RUNTIME_ID), 1)] * 2
            runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID))
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            assert runtime.last_seen_at == NOW + timedelta(seconds=300)
            assert runtime.lease_expires_at == admission.lease_expires_at == NOW + timedelta(seconds=900)
            assert admission.revision == 1
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_parallel_submit_attempts_with_same_snapshot_grant_only_one(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        automation_id = uuid4()
        canary_uid = "admission-concurrency-uid"
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
        )
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
            session.flush()
            instrument = instrument_model("admission-concurrency-instrument", str(BROKER_ID))
            instrument.external_instrument_id = canary_uid
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(
                    str(automation_id),
                    user_broker_id=str(BROKER_ID),
                    instrument_id=instrument.id,
                )
            )

        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=lambda: NOW)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-prod-1",
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
            runtime.strategy_enabled = True
        baseline = AdmissionBaseline(
            baseline_id=UUID("00000000-0000-4000-8000-000000000010"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            moscow_day=date(2026, 10, 9),
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
            scope="ACCOUNT",
            owner_runtime_instance_id=RUNTIME_ID,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.ACCOUNT_ONLY,
            captured_at=NOW - timedelta(milliseconds=500),
            valid_until=NOW + timedelta(seconds=30),
            can_arm=True,
            position_anchor_units=Decimal(0),
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=0,
            reasons=(),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000012"),
        )
        repository.save_reconciliation(reconciliation, expected_revision=2, now=NOW)
        armed = repository.arm(
            broker_id=str(BROKER_ID),
            request=ArmRequest(
                expected_revision=3,
                runtime_instance_id=RUNTIME_ID,
                baseline_id=baseline.baseline_id,
                reconciliation_id=reconciliation.reconciliation_id,
                limits=AdmissionLimits(
                    currency="RUB",
                    max_order_notional=Decimal("10000"),
                    exposure_scope="ACCOUNT_GROSS",
                    max_exposure=Decimal("20000"),
                    max_outstanding_orders=1,
                    max_daily_loss=Decimal("1000"),
                ),
                configuration_fingerprint=fingerprint,
                expires_at=NOW + timedelta(seconds=60),
            ),
            now=NOW,
            actor="operator",
        )
        requests = [
            SubmitAuthorizationRequest(
                broker_id=BROKER_ID,
                account_id="synthetic-account",
                automation_id=automation_id,
                idempotency_key=UUID(f"00000000-0000-4000-8000-{i:012d}"),
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
            for i in (21, 22)
        ]
        risk_barrier = Barrier(2)

        class FreshRisk:
            async def collect(self, _connection, **_kwargs):
                risk_barrier.wait(timeout=5)
                return AdmissionRiskSnapshot(
                    snapshot_id="00000000-0000-4000-8000-000000000017",
                    account_id="synthetic-account",
                    capture_started_at=NOW - timedelta(milliseconds=100),
                    captured_at=NOW,
                    valid_until=NOW + timedelta(seconds=1),
                    equity=Decimal("100000"),
                    equity_currency="RUB",
                    money=Decimal("1000"),
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
                    operations_through=NOW,
                    instrument=AdmissionInstrument(
                        instrument_uid=canary_uid,
                        instrument_type="SHARE",
                        currency="RUB",
                        lot_size=10,
                        price_increment=Decimal("0.01"),
                        api_trade_available=True,
                        mark_price=Decimal("10"),
                        mark_captured_at=NOW,
                    ),
                )

            async def estimate_limit_order(self, _connection, **_kwargs):
                return AdmissionOrderEstimate(Decimal("100"), Decimal("1"), "RUB", NOW)

        service._risk = FreshRisk()
        service._connection_resolver = lambda _broker_id: BrokerConnection(
            broker_id=str(BROKER_ID),
            adapter_code="t_invest",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - synthetic fixture only.
            is_test=False,
            environment="PROD",
            access_mode="TRADE",
            account_id="synthetic-account",
            configuration_fingerprint=fingerprint,
        )

        def attempt(request):
            return asyncio.run(
                service.authorize_submit(
                    request,
                    runtime_instance_id=RUNTIME_ID,
                    generation=owner.generation,
                )
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, requests))

        assert sum(isinstance(result, SubmitPermit) for result in results) == 1
        denied = next(result for result in results if isinstance(result, SubmitDenied))
        assert denied.outcome_uncertain is False
        assert denied.reason.value == "DATA_UNKNOWN"
        with factory() as session:
            count = session.scalar(select(func.count()).select_from(TradingSubmitReservationModel))
            assert count == 1

        winner = next(result for result in results if isinstance(result, SubmitPermit))
        winner_request = requests[results.index(winner)]
        with factory.begin() as session:
            broker = session.get(UserBrokerModel, str(BROKER_ID))
            broker.settings = {**broker.settings, "token": "updated-synthetic-token"}
        updated_fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="updated-synthetic-token",  # noqa: S106 - test-only changed credential.
        )
        with pytest.raises(AdmissionConflictError, match="configuration changed"):
            repository.save_baseline(
                baseline.model_copy(update={"baseline_id": UUID("00000000-0000-4000-8000-000000000025")}),
                configuration_fingerprint=fingerprint,
            )
        stale_reconciliation = ReconciliationResult(
            reconciliation_id=uuid4(),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            scope="CANARY_UID",
            owner_runtime_instance_id=RUNTIME_ID,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.CANARY_READY,
            canary_uid=canary_uid,
            captured_at=NOW,
            valid_until=NOW + timedelta(seconds=30),
            can_arm=True,
            position_anchor_units=Decimal(0),
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=1,
            reasons=(),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000018"),
        )
        with pytest.raises(AdmissionConflictError, match="configuration changed"):
            repository.save_reconciliation(
                stale_reconciliation,
                expected_revision=armed.revision,
                now=NOW,
                order_states=(),
            )

        fresh_reconciliation = stale_reconciliation.model_copy(
            update={
                "reconciliation_id": UUID("00000000-0000-4000-8000-000000000019"),
                "configuration_fingerprint": updated_fingerprint,
                "scope": "ACCOUNT",
                "status": ReconciliationStatus.ACCOUNT_ONLY,
                "canary_uid": None,
                "position_anchor_units": None,
                "unresolved_reservation_count": 1,
            }
        )
        with pytest.raises(AdmissionConflictError, match="cannot rewrite unresolved account reservations"):
            repository.save_reconciliation(
                fresh_reconciliation,
                expected_revision=armed.revision,
                now=NOW,
                order_states=(
                    AdmissionOrderState(
                        request_id=str(winner.idempotency_key),
                        broker_order_id="broker-order-1",
                        instrument_uid=canary_uid,
                        side=OrderSide.BUY,
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
        with factory() as session:
            fresh_digest = repository._reservation_digest(session, str(BROKER_ID))
        stale_worker_request = winner_request.model_copy(
            update={"idempotency_key": UUID("00000000-0000-4000-8000-000000000023")}
        )
        stale_grant = repository.grant_submit(
            stale_worker_request,
            runtime_instance_id=RUNTIME_ID,
            generation=owner.generation,
            final_clock=lambda: NOW,
            risk_snapshot_captured_at=NOW,
            risk_snapshot_valid_until=NOW + timedelta(seconds=1),
            risk_instrument=AdmissionInstrument(
                instrument_uid=canary_uid,
                instrument_type="SHARE",
                currency="RUB",
                lot_size=10,
                price_increment=Decimal("0.01"),
                api_trade_available=True,
                mark_price=Decimal("10"),
                mark_captured_at=NOW,
            ),
            risk_position_anchors=(),
            order_states=(),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000024"),
            reserved_cash=Decimal("101"),
            reserved_position_units=Decimal(0),
            requested_amount=Decimal("100"),
            expected_reservation_digest=fresh_digest,
        )
        assert isinstance(stale_grant, SubmitDenied)
        assert stale_grant.reason.value == "CONFIGURATION_CHANGED"
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(TradingSubmitReservationModel)) == 1
    finally:
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize(
    ("request_lot_size", "locked_lot_size", "expected_reason", "clock_sampled"),
    [
        (10, 10, "STALE_MARKET", True),
        (9, 10, "DATA_UNKNOWN", False),
        (10, 11, "DATA_UNKNOWN", False),
    ],
)
def test_locked_instrument_metadata_and_final_clock_guard_submit(
    isolated_postgresql_database_url, request_lot_size, locked_lot_size, expected_reason, clock_sampled
):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        canary_uid = "admission-lock-clock-uid"
        automation_id = uuid4()
        fingerprint = broker_configuration_fingerprint(
            account_id="synthetic-account",
            access_mode="TRADE",
            adapter_code="t_invest",
            broker_id=str(BROKER_ID),
            environment="PROD",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic fake credential for fingerprint checks.
        )
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
            session.flush()
            instrument = instrument_model("admission-lock-clock-instrument", str(BROKER_ID))
            instrument.external_instrument_id = canary_uid
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(str(automation_id), user_broker_id=str(BROKER_ID), instrument_id=instrument.id)
            )

        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=lambda: NOW)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-prod-1",
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
            runtime.strategy_enabled = True
        baseline = AdmissionBaseline(
            baseline_id=UUID("00000000-0000-4000-8000-000000000030"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            moscow_day=date(2026, 10, 9),
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
            reconciliation_id=UUID("00000000-0000-4000-8000-000000000031"),
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            scope="ACCOUNT",
            owner_runtime_instance_id=RUNTIME_ID,
            owner_generation=owner.generation,
            configuration_fingerprint=fingerprint,
            status=ReconciliationStatus.ACCOUNT_ONLY,
            captured_at=NOW - timedelta(milliseconds=500),
            valid_until=NOW + timedelta(seconds=30),
            can_arm=True,
            position_anchor_units=Decimal(0),
            active_ordinary_order_count=0,
            active_stop_order_count=0,
            unresolved_reservation_count=0,
            reasons=(),
            risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000032"),
        )
        repository.save_reconciliation(reconciliation, expected_revision=2, now=NOW)
        armed = repository.arm(
            broker_id=str(BROKER_ID),
            request=ArmRequest(
                expected_revision=3,
                runtime_instance_id=RUNTIME_ID,
                baseline_id=baseline.baseline_id,
                reconciliation_id=reconciliation.reconciliation_id,
                limits=AdmissionLimits(
                    currency="RUB",
                    max_order_notional=Decimal("10000"),
                    exposure_scope="ACCOUNT_GROSS",
                    max_exposure=Decimal("20000"),
                    max_outstanding_orders=1,
                    max_daily_loss=Decimal("1000"),
                ),
                configuration_fingerprint=fingerprint,
                expires_at=NOW + timedelta(seconds=60),
            ),
            now=NOW,
            actor="operator",
        )
        request = SubmitAuthorizationRequest(
            broker_id=BROKER_ID,
            account_id="synthetic-account",
            automation_id=automation_id,
            idempotency_key=uuid4(),
            generation=owner.generation,
            revision=armed.revision,
            configuration_fingerprint=fingerprint,
            instrument_uid=canary_uid,
            side="BUY",
            lot_size=request_lot_size,
            currency="RUB",
            quantity_lots=1,
            limit_price=Decimal("10"),
            market_valid_until=NOW + timedelta(seconds=15),
            submit_deadline=NOW + timedelta(seconds=14),
        )
        with factory() as session:
            digest = repository._reservation_digest(session, str(BROKER_ID))

        holder = factory()
        holder.begin()
        holder.scalar(
            select(TradingAutomationModel).where(TradingAutomationModel.id == str(automation_id)).with_for_update()
        )
        locked_instrument = holder.scalar(
            select(BrokerInstrumentModel)
            .where(BrokerInstrumentModel.external_instrument_id == canary_uid)
            .with_for_update()
        )
        if locked_lot_size != 10:
            locked_instrument.lot_size = locked_lot_size
        started = Event()
        sampled = []
        late_now = NOW + timedelta(seconds=20)
        results = []

        def attempt():
            started.set()
            results.append(
                repository.grant_submit(
                    request,
                    runtime_instance_id=RUNTIME_ID,
                    generation=owner.generation,
                    final_clock=lambda: (sampled.append(late_now) or late_now),
                    risk_snapshot_captured_at=NOW,
                    risk_snapshot_valid_until=NOW + timedelta(seconds=30),
                    risk_instrument=AdmissionInstrument(
                        instrument_uid=canary_uid,
                        instrument_type="SHARE",
                        currency="RUB",
                        lot_size=10,
                        price_increment=Decimal("0.01"),
                        api_trade_available=True,
                        mark_price=Decimal("10"),
                        mark_captured_at=NOW,
                    ),
                    risk_position_anchors=(),
                    order_states=(),
                    risk_snapshot_id=UUID("00000000-0000-4000-8000-000000000033"),
                    reserved_cash=Decimal("101"),
                    reserved_position_units=Decimal(0),
                    requested_amount=Decimal("100"),
                    expected_reservation_digest=digest,
                )
            )

        worker = Thread(target=attempt)
        worker.start()
        assert started.wait(timeout=5)
        try:
            sleep(0.2)
            assert sampled == []
        finally:
            if locked_lot_size == 10:
                holder.rollback()
            else:
                holder.commit()
            holder.close()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert sampled == ([late_now] if clock_sampled else [])
        assert len(results) == 1
        assert isinstance(results[0], SubmitDenied)
        assert results[0].reason.value == expected_reason
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(TradingSubmitReservationModel)) == 0
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_owner_renewal_after_lease_expiry_waits_for_account_lock(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), "synthetic-account")
            broker.environment = "PROD"
            session.add(broker)

        repository = TradingAdmissionRepository(factory)
        service = TradingAdmissionService(repository, clock=lambda: NOW, owner_lease_seconds=10)
        owner = service.register_runtime(
            RuntimeRegistrationRequest(
                worker_id="worker-prod-lease",
                runtime_instance_id=RUNTIME_ID,
                broker_id=BROKER_ID,
                application_environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
            ),
            "a" * 64,
        )

        release_holder = Event()
        clock_calls = []

        def clock():
            now = NOW + timedelta(seconds=11 if release_holder.is_set() else 9)
            clock_calls.append(now)
            return now

        service._clock = clock
        holder = factory()
        holder.begin()
        holder.scalar(select(UserBrokerModel).where(UserBrokerModel.id == str(BROKER_ID)).with_for_update())
        started = Event()
        outcomes = []

        def renew():
            started.set()
            try:
                outcomes.append(service.renew_owner(RUNTIME_ID, owner.generation))
            except Exception as error:
                outcomes.append(error)

        worker = Thread(target=renew)
        worker.start()
        assert started.wait(timeout=5)
        try:
            sleep(0.2)
            assert clock_calls == []
        finally:
            holder.rollback()
            holder.close()
            release_holder.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert clock_calls == [NOW + timedelta(seconds=11)]
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], AdmissionConflictError)
        with factory() as session:
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            assert admission.admission_status == "DISARMED"
            assert admission.lease_expires_at == NOW + timedelta(seconds=10)
    finally:
        engine.dispose()
