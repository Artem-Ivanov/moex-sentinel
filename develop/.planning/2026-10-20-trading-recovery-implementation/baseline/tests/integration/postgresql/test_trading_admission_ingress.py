"""Real Worker outbox recovery through authenticated Core HTTP and PostgreSQL."""

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
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import AutomationEventModel, Base, BrokerOrderModel, TradingAutomationModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from sentinel_contracts.trading_admission import OwnerStoppedRequest, PreRpcAbortProof
from sentinel_contracts.trading_facts import FactEnvelope
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    INSTRUMENT_A,
    ORDER_ID,
    RUNTIME_A,
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
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
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
