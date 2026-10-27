"""Real Worker outbox recovery through authenticated Core HTTP and PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.api import app as app_module
from moex_sentinel.domain.trading_admission import AdmissionOrderState
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOrderEstimate,
    AdmissionPosition,
    AdmissionRiskSnapshot,
)
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import AutomationEventModel, Base, BrokerOrderModel, TradingAutomationModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.repositories.trading_admission import (
    WORKER_UNCERTAIN_HOLD_REASON,
    TradingAdmissionRepository,
)
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    OwnerStoppedRequest,
    PreRpcAbortProof,
    SubmitAuthorizationRequest,
    SubmitPermit,
    UpdateLimitsRequest,
)
from sentinel_contracts.trading_facts import FactEnvelope
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    INSTRUMENT_A,
    NOW,
    ORDER_ID,
    RUNTIME_A,
    RUNTIME_B,
    SCOPE_ID,
    SUBMIT_KEY,
    admit_order_prefix,
    production_fact_service,
    proof_fact,
    seed_fact_submit_reservation,
    valid_abort_proof,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import INTENT_ID, baseline_command
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.domain.storage_dtos import ExecutionFinalization
from trading_automaton.services.automation_lifecycle import WorkerAutomationLifecycleService
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
from trading_automaton.storage.models import CachedAutomationModel
from trading_automaton.storage.repository import LocalAutomationRepository

pytestmark = pytest.mark.postgresql

WORKER_TOKEN = "a" * 64
REQUEST_FINGERPRINT = "c" * 64
CONFIGURATION_FINGERPRINT = "d" * 64
PERMIT_ID = UUID("00000000-0000-4000-8000-000000000711")
_FACT_ADAPTER = TypeAdapter(FactEnvelope)


def _now() -> datetime:
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def _core_transport(core: TestClient, *, lose_fact_ack: Event) -> httpx.MockTransport:
    def send(request: httpx.Request) -> httpx.Response:
        response = core.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            headers=dict(request.headers),
            content=request.content,
        )
        if request.url.path == "/internal/automation-facts" and lose_fact_ack.is_set():
            lose_fact_ack.clear()
            raise httpx.ReadTimeout("synthetic response lost after Core commit", request=request)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)

    return httpx.MockTransport(send)


def _envelopes(rows):
    return [
        _FACT_ADAPTER.validate_python(
            {
                "event_id": row.event_id,
                "user_broker_id": row.user_broker_id,
                "automation_id": row.automation_id,
                "sequence_number": row.sequence_number,
                "expected_revision": row.expected_revision,
                "fact_kind": row.fact_kind,
                "payload": row.payload,
                "safe_message": row.safe_message,
                "occurred_at": row.occurred_at,
            }
        )
        for row in rows
    ]


def _client(core: TestClient, lost_ack: Event, broker_id: UUID) -> tuple[CoreClient, httpx.Client]:
    http = httpx.Client(transport=_core_transport(core, lose_fact_ack=lost_ack), base_url="http://core.test")
    return (
        CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-prod-1",
            broker_id=broker_id,
            worker_token=WORKER_TOKEN,
        ),
        http,
    )


def test_worker_proof_outbox_replays_after_lost_ack_and_runtime_transfer(
    isolated_postgresql_database_url, tmp_path, monkeypatch
) -> None:
    worker_command = baseline_command()
    broker_id = worker_command.user_broker_id
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("INTERNAL_WORKER_ID", "worker-prod-1")
    monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(broker_id))
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://example.test")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-test-core-session")

    scope_id = str(worker_command.user_broker_id)
    automation_id = str(worker_command.automation_id)
    instrument_id = str(worker_command.instrument_id)
    engine = create_database_engine(isolated_postgresql_database_url)
    with sessionmaker(engine)() as seed_session, seed_session.begin():
        scope = user_broker_model(scope_id, worker_command.account_id)
        scope.environment = "PROD"
        scope.fqdn = "invest-public-api.tbank.ru:443"
        seed_session.add(scope)
        seed_session.flush()
        instrument = instrument_model(instrument_id, scope_id)
        instrument.external_instrument_id = worker_command.external_instrument_id
        instrument.ticker = "SYNTH"
        instrument.lot_size = worker_command.lot_size
        seed_session.add(instrument)
        seed_session.flush()
        seed_session.add(automation_model(automation_id, user_broker_id=scope_id, instrument_id=instrument_id))

    worker_db = f"sqlite:///{tmp_path / 'worker-outbox.db'}"
    worker_engine = create_worker_engine(worker_db)
    WorkerBase.metadata.create_all(worker_engine)
    worker_factory = sessionmaker(worker_engine, expire_on_commit=False)
    worker_repository = LocalAutomationRepository(
        worker_factory,
        fact_writer=FactOutboxWriter(clock=_now),
    )
    worker_repository.cache_command(worker_command)
    worker_repository.save_decision_batch((decision_item(worker_command, intent_id=INTENT_ID),), occurred_at=_now())

    entered_ingress = Event()
    release_ingress = Event()
    pause_ingress = Event()
    original_publish = TradingFactIngressService.publish

    def pause_after_authentication(service, facts, *, runtime_instance_id=None, generation=None):
        if pause_ingress.is_set():
            entered_ingress.set()
            if not release_ingress.wait(10):
                raise TimeoutError("test did not release paused fact ingress")
        result = original_publish(
            service,
            facts,
            runtime_instance_id=runtime_instance_id,
            generation=generation,
        )
        if not pause_ingress.is_set():
            assert result.failures == (), result.failures
        return result

    application = app_module.create_app(database_url=isolated_postgresql_database_url, test_auth_bypass=True)
    reopened_engine = None
    http_a = http_b = None
    try:
        with TestClient(application) as core:
            lost_ack = Event()
            client_a, http_a = _client(core, lost_ack, broker_id)
            client_b, http_b = _client(core, Event(), broker_id)
            client_a.validate_runtime()
            owner_a = client_a._registration
            assert owner_a is not None
            assert owner_a.registration_status.value == "OWNER"
            with pytest.raises(CoreOperationError) as pending_registration:
                client_b.validate_runtime()
            assert pending_registration.value.retryable
            pending_b = client_b._registration
            assert pending_b is not None
            assert pending_b.registration_status.value == "PENDING"
            with pytest.raises(CoreOperationError) as pending_publish:
                client_b.publish_facts([])
            assert pending_publish.value.retryable

            now = _now()
            proof = PreRpcAbortProof(
                permit_id=PERMIT_ID,
                idempotency_key=UUID(INTENT_ID),
                request_fingerprint=REQUEST_FINGERPRINT,
                runtime_instance_id=owner_a.runtime_instance_id,
                generation=owner_a.generation,
                configuration_fingerprint=CONFIGURATION_FINGERPRINT,
                aborted_at=now,
                reason="PERMIT_EXPIRED_BEFORE_POST",
            )
            with application.state.session_factory.begin() as session:
                session.add(
                    TradingSubmitReservationModel(
                        reservation_id=str(UUID(INTENT_ID)),
                        user_broker_id=scope_id,
                        automation_id=automation_id,
                        idempotency_key=INTENT_ID,
                        request_fingerprint=REQUEST_FINGERPRINT,
                        state="IN_FLIGHT",
                        runtime_instance_id=str(owner_a.runtime_instance_id),
                        generation=owner_a.generation,
                        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
                        permit_id=str(PERMIT_ID),
                        side="BUY",
                        instrument_uid=worker_command.external_instrument_id,
                        quantity_lots=1,
                        limit_price=Decimal("100"),
                        requested_amount=Decimal("1000"),
                        reserved_cash=Decimal("1001"),
                        reserved_position_units=Decimal(0),
                        payload={
                            "anchor_executed_lots": 0,
                            "executed_lots": 0,
                            "executed_amount": "0",
                            "executed_commission": "0",
                            "original_reserved_cash": "1001",
                            "last_reconciled_order_state": None,
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
            with application.state.session_factory() as session:
                admission = session.get(TradingAccountAdmissionModel, scope_id)
                stored_owner = session.get(WorkerRuntimeInstanceModel, str(owner_a.runtime_instance_id))
                assert admission.owner_runtime_instance_id == str(owner_a.runtime_instance_id)
                assert admission.owner_generation == owner_a.generation
                assert stored_owner.registration_status == "OWNER"

            worker_repository.update_intent(
                INTENT_ID,
                state="SUBMITTING",
                occurred_at=now,
                dispatch_started_at=now,
            )
            worker_repository.update_intent(
                INTENT_ID,
                state="CANCELLED",
                occurred_at=now + timedelta(milliseconds=1),
                requested_amount=Decimal("1000"),
                estimated_commission=Decimal(),
                pre_rpc_abort_proof=proof,
            )
            worker_engine.dispose()
            reopened_engine = create_worker_engine(worker_db)
            reopened = LocalAutomationRepository(
                sessionmaker(reopened_engine, expire_on_commit=False),
                fact_writer=FactOutboxWriter(clock=lambda: _now() + timedelta(seconds=2)),
            )
            worker_facts = reopened.ready_fact_outbox(100, now=_now() + timedelta(seconds=2), deadline_ms=0)
            assert worker_facts
            order_transitions = [fact for fact in worker_facts if "to_state" in fact.payload]
            assert [(fact.payload["from_state"], fact.payload["to_state"]) for fact in order_transitions] == [
                ("DISPATCH_PENDING", "SUBMITTING"),
                ("SUBMITTING", "CANCELLED"),
            ]
            assert order_transitions[0].payload.get("pre_rpc_abort_proof") is None
            assert order_transitions[1].payload["estimated_commission"] == "0"
            assert order_transitions[1].payload["pre_rpc_abort_proof"] == proof.model_dump(mode="json")
            assert worker_facts[-1].payload["pre_rpc_abort_proof"] == proof.model_dump(mode="json")
            envelopes = _envelopes(worker_facts)

            monkeypatch.setattr(TradingFactIngressService, "publish", pause_after_authentication)
            lost_ack.set()
            with pytest.raises(CoreOperationError) as lost_response:
                client_a.publish_facts(envelopes)
            assert lost_response.value.retryable
            with application.state.session_factory() as session:
                accepted_events = session.scalar(select(func.count()).select_from(AutomationEventModel))
                stored_automation = session.get(TradingAutomationModel, automation_id)
                admission = application.state.usecases.trading_admission.view(scope_id)
            assert accepted_events == len(envelopes)
            assert stored_automation is not None
            assert stored_automation.last_sequence_number == worker_facts[-1].sequence_number

            pause_ingress.set()
            with ThreadPoolExecutor(max_workers=1) as pool:
                stale_publish = pool.submit(client_a.publish_facts, envelopes)
                assert entered_ingress.wait(5), "request did not authenticate before owner transfer"
                application.state.usecases.trading_admission.owner_stopped(
                    scope_id,
                    OwnerStoppedRequest(
                        expected_revision=admission.revision,
                        old_runtime_instance_id=owner_a.runtime_instance_id,
                        old_generation=owner_a.generation,
                        candidate_runtime_instance_id=client_b.runtime_instance_id,
                        stopped_at=_now() - timedelta(seconds=1),
                        auto_restart_disabled=True,
                        evidence_ref="test:worker-stop-attestation",
                    ),
                    actor="integration-test",
                )
                release_ingress.set()
                stale_result = stale_publish.result(timeout=10)
            assert len(stale_result.failures) == 1
            assert stale_result.failures[0].retryable

            client_b.validate_runtime()
            owner_b = client_b._registration
            assert owner_b is not None
            assert owner_b.registration_status.value == "OWNER"
            recovery_result = client_b.publish_facts(envelopes)
            assert recovery_result.failures == ()
            assert len(recovery_result.results) == 1

            with application.state.session_factory() as session:
                assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == accepted_events
                reservation = session.get(TradingSubmitReservationModel, str(UUID(INTENT_ID)))
                assert reservation is not None
                assert reservation.state == "CONFIRMED_NOT_SENT"
                assert reservation.reserved_cash == Decimal(0)
                assert reservation.reserved_position_units == Decimal(0)

            acknowledgement = recovery_result.results[0]
            reopened.acknowledge_fact_outbox(
                automation_id,
                accepted_through_sequence=acknowledgement.accepted_through_sequence,
                current_revision=acknowledgement.current_revision,
            )
            assert reopened.ready_fact_outbox(100, now=_now() + timedelta(seconds=5), deadline_ms=0) == []
            assert client_a.runtime_instance_id == owner_a.runtime_instance_id
            assert client_b.runtime_instance_id == owner_b.runtime_instance_id
    finally:
        if http_a is not None:
            http_a.close()
        if http_b is not None:
            http_b.close()
        worker_engine.dispose()
        if reopened_engine is not None:
            reopened_engine.dispose()
        engine.dispose()


def _seed_postgresql_fact_ingress(factory) -> None:
    with factory.begin() as session:
        broker = user_broker_model(str(SCOPE_ID), "synthetic-account")
        broker.environment = "PROD"
        broker.fqdn = "invest-public-api.tbank.ru:443"
        session.add(broker)
        session.flush()
        instrument = instrument_model(str(INSTRUMENT_A), str(SCOPE_ID))
        instrument.ticker = "SYNTH"
        instrument.name = "Synthetic"
        session.add(instrument)
        session.flush()
        session.add(
            automation_model(
                str(AUTOMATION_A),
                user_broker_id=str(SCOPE_ID),
                instrument_id=str(INSTRUMENT_A),
            )
        )
    seed_fact_submit_reservation(factory)
    admit_order_prefix(factory)


def test_real_worker_age_event_is_accepted_as_core_candidate(isolated_postgresql_database_url, tmp_path) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    created_at = NOW - timedelta(hours=25)
    worker_engine = None
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(SCOPE_ID), "synthetic-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
            session.flush()
            instrument = instrument_model(str(INSTRUMENT_A), str(SCOPE_ID))
            instrument.external_instrument_id = "external-canary"
            instrument.ticker = "SYNTH"
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(
                    str(AUTOMATION_A),
                    user_broker_id=str(SCOPE_ID),
                    instrument_id=str(INSTRUMENT_A),
                )
            )
        seed_fact_submit_reservation(factory)
        with factory.begin() as session:
            session.add(
                WorkerRuntimeInstanceModel(
                    runtime_instance_id=str(RUNTIME_B),
                    user_broker_id=str(SCOPE_ID),
                    worker_id="worker-prod-recovery",
                    environment="PROD",
                    access_mode="TRADE",
                    strategy_enabled=True,
                    secret_digest="e" * 64,
                    registration_status="PENDING",
                    generation=None,
                    registered_at=NOW,
                    last_seen_at=NOW,
                    lease_expires_at=None,
                    retired_at=None,
                )
            )

        worker_command = baseline_command().model_copy(
            update={
                "automation_id": AUTOMATION_A,
                "user_broker_id": SCOPE_ID,
                "instrument_id": INSTRUMENT_A,
                "external_instrument_id": "external-canary",
                "broker_id": str(SCOPE_ID),
                "account_id": "synthetic-account",
            }
        )
        worker_db = f"sqlite:///{tmp_path / 'worker-age-policy.db'}"
        worker_engine = create_worker_engine(worker_db)
        WorkerBase.metadata.create_all(worker_engine)
        repository = LocalAutomationRepository(
            sessionmaker(worker_engine, expire_on_commit=False),
            fact_writer=FactOutboxWriter(clock=lambda: NOW),
        )
        repository.cache_command(worker_command)
        repository.save_decision_batch(
            (decision_item(worker_command, intent_id=str(SUBMIT_KEY)),),
            occurred_at=created_at,
        )
        repository.update_intent(
            str(SUBMIT_KEY),
            state="SUBMITTING",
            occurred_at=created_at + timedelta(milliseconds=1),
            dispatch_started_at=created_at + timedelta(milliseconds=1),
        )
        prefix = repository.ready_fact_outbox(100, now=NOW + timedelta(seconds=2), deadline_ms=0)
        assert prefix
        prefix_result = production_fact_service(factory).publish(
            _envelopes(prefix), runtime_instance_id=RUNTIME_A, generation=1
        )
        assert prefix_result.failures == (), prefix_result.failures
        repository.acknowledge_fact_outbox(
            str(AUTOMATION_A),
            accepted_through_sequence=prefix_result.results[0].accepted_through_sequence,
            current_revision=prefix_result.results[0].current_revision,
        )

        assert repository.transition_state(
            automation_id=str(AUTOMATION_A),
            state=AutomationState.HOLD,
            safe_message=WORKER_UNCERTAIN_HOLD_REASON,
            occurred_at=NOW,
        )

        # Simulate a completed trusted recovery on the new generation. The order
        # reservation remains unresolved until its age fact is received.
        core_repository = TradingAdmissionRepository(factory)
        core_service = TradingAdmissionService(core_repository, clock=lambda: NOW + timedelta(seconds=1))
        core_service.owner_stopped(
            str(SCOPE_ID),
            OwnerStoppedRequest(
                expected_revision=1,
                old_runtime_instance_id=RUNTIME_A,
                old_generation=1,
                candidate_runtime_instance_id=RUNTIME_B,
                stopped_at=NOW,
                auto_restart_disabled=True,
                evidence_ref="test:retired-generation-age-fact",
            ),
            actor="integration-test",
        )
        with factory.begin() as session:
            admission = session.get_one(TradingAccountAdmissionModel, str(SCOPE_ID))
            admission.recovery_only = False
            admission.recovery_runtime_instance_id = None

        aged = repository.expire_intent_by_age(str(SUBMIT_KEY), expired_at=NOW)
        assert aged is not None
        assert aged.terminal_at is None
        assert aged.strategy_snapshot["_age_policy"]["expired_at"] == NOW.isoformat(timespec="milliseconds")
        aged_rows = repository.ready_fact_outbox(100, now=NOW + timedelta(seconds=1), deadline_ms=0)
        assert len(aged_rows) == 2
        assert aged_rows[0].occurred_at == NOW
        assert aged_rows[1].occurred_at == NOW + timedelta(milliseconds=1)
        payload = aged_rows[1].payload
        assert payload["occurred_at"] == NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        assert payload["safe_reason"] == "ORDER_AGE_LIMIT_EXCEEDED"
        assert payload["from_state"] == "SUBMITTING"
        assert payload["to_state"] == "FAILED"
        assert payload["terminal_at"] is None
        assert "_age_policy" not in payload["strategy_snapshot"]

        accepted = production_fact_service(factory).publish(
            _envelopes(aged_rows), runtime_instance_id=RUNTIME_B, generation=2
        )
        assert accepted.failures == (), accepted.failures
        with factory() as session:
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            assert reservation is not None
            order = session.get_one(BrokerOrderModel, str(SUBMIT_KEY))
            assert (order.state, order.terminal_at) == ("FAILED", None)
            assert reservation.state == "IN_FLIGHT"
            assert reservation.payload["_age_policy"] == {
                "version": 1,
                "expired_at": NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }
            assert reservation.reserved_cash == Decimal("1001")
    finally:
        if worker_engine is not None:
            worker_engine.dispose()
        engine.dispose()


def test_real_worker_age_recovery_resumes_through_queue_after_core_request(
    isolated_postgresql_database_url, tmp_path
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    worker_engine = None
    try:
        with factory.begin() as session:
            broker = user_broker_model(str(SCOPE_ID), "synthetic-account")
            broker.environment = "PROD"
            broker.fqdn = "invest-public-api.tbank.ru:443"
            session.add(broker)
            session.flush()
            instrument = instrument_model(str(INSTRUMENT_A), str(SCOPE_ID))
            instrument.external_instrument_id = "external-canary"
            instrument.ticker = "SYNTH"
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(
                    str(AUTOMATION_A), user_broker_id=str(SCOPE_ID), instrument_id=str(INSTRUMENT_A)
                )
            )
        seed_fact_submit_reservation(factory)
        with factory.begin() as session:
            session.add(
                WorkerRuntimeInstanceModel(
                    runtime_instance_id=str(RUNTIME_B),
                    user_broker_id=str(SCOPE_ID),
                    worker_id="worker-prod-recovery",
                    environment="PROD",
                    access_mode="TRADE",
                    strategy_enabled=True,
                    secret_digest="e" * 64,
                    registration_status="PENDING",
                    generation=None,
                    registered_at=NOW,
                    last_seen_at=NOW,
                    lease_expires_at=None,
                    retired_at=None,
                )
            )

        worker_command = baseline_command().model_copy(
            update={
                "automation_id": AUTOMATION_A,
                "user_broker_id": SCOPE_ID,
                "instrument_id": INSTRUMENT_A,
                "external_instrument_id": "external-canary",
                "broker_id": str(SCOPE_ID),
                "account_id": "synthetic-account",
            }
        )
        worker_engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker-age-recovery.db'}")
        WorkerBase.metadata.create_all(worker_engine)
        repository = LocalAutomationRepository(
            sessionmaker(worker_engine, expire_on_commit=False),
            fact_writer=FactOutboxWriter(clock=lambda: NOW),
        )
        repository.cache_command(worker_command)
        created_at = NOW - timedelta(hours=25)
        repository.save_decision_batch(
            (decision_item(worker_command, intent_id=str(SUBMIT_KEY)),), occurred_at=created_at
        )
        repository.update_intent(
            str(SUBMIT_KEY),
            state="SUBMITTING",
            occurred_at=created_at + timedelta(milliseconds=1),
            dispatch_started_at=created_at + timedelta(milliseconds=1),
        )
        assert repository.transition_state(
            automation_id=str(AUTOMATION_A),
            state=AutomationState.HOLD,
            safe_message=WORKER_UNCERTAIN_HOLD_REASON,
            occurred_at=NOW,
        )

        ingress = production_fact_service(factory)

        class CoreClientStub:
            commands = []
            runtime_instance_id = RUNTIME_A
            generation = 1

            def claim_commands(self, _worker_id, _limit):
                return self.commands

            def publish_facts(self, facts):
                return ingress.publish(
                    facts,
                    runtime_instance_id=self.runtime_instance_id,
                    generation=self.generation,
                )

            def automation_statuses(self, _automation_ids):
                raise AssertionError("A successful recovery fact must not need status reconciliation.")

        client = CoreClientStub()
        synchronization = FactSynchronizationService(
            repository, client, now=lambda: NOW + timedelta(seconds=1), sleep=lambda _seconds: None, deadline_ms=0
        )

        def deliver_outbox() -> None:
            rows = repository.ready_fact_outbox(100, now=NOW + timedelta(seconds=2), deadline_ms=0)
            assert rows
            result = client.publish_facts(_envelopes(rows))
            assert result.failures == (), result.failures
            acknowledgement = result.results[0]
            repository.acknowledge_fact_outbox(
                str(acknowledgement.automation_id),
                accepted_through_sequence=acknowledgement.accepted_through_sequence,
                current_revision=acknowledgement.current_revision,
            )

        # A genuine Worker age sweep creates the FAILED / terminal-unknown fact.
        # Its payload retains expiry time even when the outbox advances the envelope
        # timestamp to keep the event stream monotonic.
        assert repository.expire_intent_by_age(str(SUBMIT_KEY), expired_at=NOW) is not None
        # Controlled owner transfer changes the current authenticated sender while
        # preserving the original intent's retired generation binding.
        core_repository = TradingAdmissionRepository(factory)
        core_service = TradingAdmissionService(
            core_repository,
            clock=lambda: NOW + timedelta(seconds=10),
        )
        core_service.owner_stopped(
            str(SCOPE_ID),
            OwnerStoppedRequest(
                expected_revision=1,
                old_runtime_instance_id=RUNTIME_A,
                old_generation=1,
                candidate_runtime_instance_id=RUNTIME_B,
                stopped_at=NOW + timedelta(seconds=1),
                auto_restart_disabled=True,
                evidence_ref="test:controlled-owner-transfer",
            ),
            actor="integration-test",
        )
        client.runtime_instance_id = RUNTIME_B
        client.generation = 2
        deliver_outbox()
        with factory() as session:
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            assert reservation is not None
            assert reservation.state == "IN_FLIGHT"
            assert reservation.payload["_age_policy"]["version"] == 1
            assert reservation.payload.get("settlement_reason") is None

        owner_view = core_repository.get_view(str(SCOPE_ID), now=NOW + timedelta(seconds=10))
        connection = BrokerConnection(
            broker_id=str(SCOPE_ID),
            adapter_code="t_invest",
            target="invest-public-api.tbank.ru:443",
            token="synthetic-token",  # noqa: S106 - deterministic fake credential for an isolated test.
            is_test=False,
            environment="PROD",
            access_mode="TRADE",
            account_id="synthetic-account",
            configuration_fingerprint=owner_view.configuration_fingerprint,
        )

        class FreshRisk:
            async def collect(self, _connection, *, request_ids=(), **_kwargs):
                unknown_states = tuple(
                    AdmissionOrderState(
                        request_id=request_id,
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
                        unknown_reason="REQUEST_NOT_FOUND",
                    )
                    for request_id in request_ids
                )
                instrument = (
                    None
                    if not request_ids
                    else AdmissionInstrument(
                        instrument_uid="external-canary",
                        instrument_type="SHARE",
                        currency="RUB",
                        lot_size=10,
                        price_increment=Decimal("0.01"),
                        api_trade_available=True,
                        mark_price=Decimal("100"),
                        mark_captured_at=NOW + timedelta(seconds=10),
                    )
                )
                return AdmissionRiskSnapshot(
                    snapshot_id=(
                        "00000000-0000-4000-8000-000000000801"
                        if not request_ids
                        else "00000000-0000-4000-8000-000000000802"
                    ),
                    account_id="synthetic-account",
                    capture_started_at=NOW + timedelta(seconds=7),
                    captured_at=NOW + timedelta(seconds=8),
                    valid_until=NOW + timedelta(seconds=25),
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
                    order_states=unknown_states,
                    operations=(),
                    operations_complete=True,
                    operations_through=NOW + timedelta(seconds=8),
                    instrument=instrument,
                    unknown_reasons=tuple("REQUEST_NOT_FOUND" for _ in request_ids),
                    instruments=() if instrument is None else (instrument,),
                )

        core_service._risk = FreshRisk()
        core_service._connection_resolver = lambda _broker_id: connection
        daily = asyncio.run(core_service.prepare_daily(str(SCOPE_ID)))
        if not core_repository.needs_age_policy_recovery(str(SCOPE_ID)):
            pytest.fail("Age-policy candidate was not retained for automatic recovery.")
        with factory() as session:
            core_automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
            admission = session.get_one(TradingAccountAdmissionModel, str(SCOPE_ID))
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            assert core_automation.state == "HOLD", daily
            assert core_automation.resume_requested, daily
            assert core_automation.suspended_from_state == "IN_WORK"
            assert admission.recovery_only is False
            assert reservation.state == "SETTLED"
            assert reservation.payload["settlement_reason"] == "AGE_POLICY"
            requested = worker_command.model_copy(
                update={
                    "state": AutomationState.HOLD,
                    "revision": core_automation.revision,
                    "last_sequence_number": core_automation.last_sequence_number,
                    "resume_requested": True,
                }
            )
            limits = AdmissionLimits(
                currency="RUB",
                max_order_notional=Decimal("10000"),
                max_exposure=Decimal("50000"),
                max_outstanding_orders=3,
                max_daily_loss=Decimal("1000"),
            )
        admission_view = core_service.view(str(SCOPE_ID))
        core_service.update_limits(
            str(SCOPE_ID),
            UpdateLimitsRequest(expected_revision=admission_view.revision, limits=limits),
            actor="integration-test",
        )
        admission_view = core_service.view(str(SCOPE_ID))
        assert admission_view.baseline is not None
        assert admission_view.reconciliation is not None
        assert admission_view.admission_status.value == "DISARMED"

        client.commands = [requested]
        assert synchronization.claim_commands("worker-prod-1", 10) == []
        with worker_engine.connect() as worker_connection:
            cached = worker_connection.execute(
                select(
                    CachedAutomationModel.state,
                    CachedAutomationModel.revision,
                    CachedAutomationModel.last_sequence_number,
                )
                .where(CachedAutomationModel.automation_id == str(AUTOMATION_A))
            ).one()
            assert cached.state == AutomationState.IN_QUEUE.value
        # Core accepts the typed HOLD→IN_QUEUE proposal before the queue can activate.
        deliver_outbox()

        with factory() as session:
            core_automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
            assert (core_automation.state, core_automation.resume_requested) == ("IN_QUEUE", False)
            queued = worker_command.model_copy(
                update={
                    "state": AutomationState.IN_QUEUE,
                    "revision": core_automation.revision,
                    "last_sequence_number": core_automation.last_sequence_number,
                    "resume_requested": False,
                }
            )
        client.commands = [queued]
        assert synchronization.claim_commands("worker-prod-1", 10) == [queued]
        assert WorkerAutomationLifecycleService(repository).propose(
            automation_id=str(AUTOMATION_A),
            target=AutomationState.IN_WORK,
            safe_message="Automation claimed by streaming worker",
            occurred_at=NOW + timedelta(seconds=5),
        )
        deliver_outbox()
        with factory() as session:
            core_automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
            assert (core_automation.state, core_automation.resume_requested) == ("IN_WORK", False)

        # A real Worker terminal fill arrives after Core age-policy settlement.
        # Core reconciles its durable settlement projection, then authorizes a new
        # order using the fresh broker position and the existing reconciliation.
        finalization = ExecutionFinalization(
            intent_id=str(SUBMIT_KEY),
            automation_id=str(AUTOMATION_A),
            broker_id=str(SCOPE_ID),
            account_id="synthetic-account",
            instrument_id="external-canary",
            side="BUY",
            quantity_lots=1,
            requested_price=Decimal("100"),
            currency="RUB",
            state="FILLED",
            occurred_at=NOW + timedelta(seconds=6),
            broker_order_id="late-fill-order",
            requested_amount=Decimal("1000"),
            executed_amount=Decimal("1000"),
            estimated_commission=Decimal("1"),
            executed_commission=Decimal("2"),
            executed_lots=1,
            executed_price=Decimal("100"),
            executed_at=NOW + timedelta(seconds=6),
            terminal_at=NOW + timedelta(seconds=6),
            lot_size=10,
            process_id="00000000-0000-4000-8000-000000000803",
        )
        assert repository.finalize_execution(finalization).applied
        assert not repository.finalize_execution(finalization).applied
        deliver_outbox()
        with factory() as session:
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            assert reservation.state == "SETTLED"
            assert reservation.payload["settlement_reason"] == "AGE_POLICY"
            assert reservation.payload["broker_outcome_unknown"] is False
            assert reservation.payload["executed_lots"] == 1
            assert reservation.payload["anchor_reconciliation_id"] == str(
                core_service.view(str(SCOPE_ID)).reconciliation.reconciliation_id
            )
            assert reservation.payload["anchor_executed_lots"] == 0

        class FreshRiskWithPosition(FreshRisk):
            async def collect(self, _connection, *, request_ids=(), instrument_uid=None, **_kwargs):
                snapshot = await super().collect(_connection, request_ids=request_ids)
                instrument = (
                    None
                    if instrument_uid is None
                    else AdmissionInstrument(
                        instrument_uid="external-canary",
                        instrument_type="SHARE",
                        currency="RUB",
                        lot_size=10,
                        price_increment=Decimal("0.01"),
                        api_trade_available=True,
                        mark_price=Decimal("100"),
                        mark_captured_at=NOW + timedelta(seconds=10),
                    )
                )
                return snapshot.__class__(
                    **{
                        **snapshot.__dict__,
                        "capture_started_at": NOW + timedelta(seconds=9),
                        "captured_at": NOW + timedelta(seconds=10),
                        "valid_until": NOW + timedelta(seconds=25),
                        "operations_through": NOW + timedelta(seconds=10),
                        "positions": (
                            ()
                            if instrument_uid is None
                            else (
                                AdmissionPosition(
                                    instrument_uid="external-canary",
                                    quantity_units=Decimal("10"),
                                    blocked_units=Decimal(0),
                                    mark_price=Decimal("100"),
                                    currency="RUB",
                                    lot_size=10,
                                    mark_captured_at=NOW + timedelta(seconds=10),
                                ),
                            )
                        ),
                        "instrument": instrument,
                        "instruments": () if instrument is None else (instrument,),
                    }
                )

            async def estimate_limit_order(self, *_args, **_kwargs):
                return AdmissionOrderEstimate(Decimal("1000"), Decimal("1"), "RUB", NOW + timedelta(seconds=10))

        core_service._risk = FreshRiskWithPosition()
        permit = asyncio.run(
            core_service.authorize_submit(
                SubmitAuthorizationRequest(
                    broker_id=SCOPE_ID,
                    account_id="synthetic-account",
                    automation_id=AUTOMATION_A,
                    idempotency_key=UUID("00000000-0000-4000-8000-000000000804"),
                    generation=2,
                    revision=core_service.view(str(SCOPE_ID)).revision,
                    configuration_fingerprint=core_service.view(str(SCOPE_ID)).configuration_fingerprint,
                    instrument_uid="external-canary",
                    lot_size=10,
                    currency="RUB",
                    side="BUY",
                    quantity_lots=1,
                    limit_price=Decimal("100"),
                    market_valid_until=NOW + timedelta(seconds=25),
                    submit_deadline=NOW + timedelta(seconds=20),
                ),
                runtime_instance_id=RUNTIME_B,
                generation=2,
            )
        )
        assert isinstance(permit, SubmitPermit)
    finally:
        if worker_engine is not None:
            worker_engine.dispose()
        engine.dispose()


def test_postgresql_ingress_accepts_and_replays_exact_pre_rpc_proof(isolated_postgresql_database_url) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        _seed_postgresql_fact_ingress(factory)

        service = production_fact_service(factory)
        fact = proof_fact(proof=valid_abort_proof())
        accepted = service.publish([fact], runtime_instance_id=RUNTIME_A, generation=1)
        replay = service.publish([fact], runtime_instance_id=RUNTIME_A, generation=1)

        assert accepted.failures == replay.failures == ()
        assert replay.results[0].accepted_event_ids == (fact.event_id,)
        with factory() as session:
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
            assert reservation.state == "CONFIRMED_NOT_SENT"
            assert reservation.reserved_cash == reservation.reserved_position_units == 0
            assert (automation.last_sequence_number, automation.revision) == (5, 1)
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 5
    finally:
        engine.dispose()


def test_postgresql_ingress_rejects_preexisting_core_order_attribution(isolated_postgresql_database_url) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        _seed_postgresql_fact_ingress(factory)
        with factory.begin() as session:
            order = session.get_one(BrokerOrderModel, str(ORDER_ID))
            order.external_order_id = "preexisting-broker-order"

        result = production_fact_service(factory).publish(
            [proof_fact(proof=valid_abort_proof())], runtime_instance_id=RUNTIME_A, generation=1
        )

        assert result.results == ()
        assert result.failures[0].code.value == "INVALID_FACT_STATE"
        with factory() as session:
            reservation = session.scalar(
                select(TradingSubmitReservationModel).where(
                    TradingSubmitReservationModel.idempotency_key == str(SUBMIT_KEY)
                )
            )
            order = session.get_one(BrokerOrderModel, str(ORDER_ID))
            automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
            assert reservation.state == "IN_FLIGHT"
            assert reservation.reserved_cash == Decimal("1001")
            assert order.external_order_id == "preexisting-broker-order"
            assert (automation.last_sequence_number, automation.revision) == (4, 1)
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4
    finally:
        engine.dispose()
