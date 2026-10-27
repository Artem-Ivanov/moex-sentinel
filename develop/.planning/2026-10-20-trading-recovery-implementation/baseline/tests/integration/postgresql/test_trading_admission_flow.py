"""Real PROD TRADE enrollment, recovery, and submit flow over Core HTTP."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy.orm import sessionmaker

from moex_sentinel.api import app as app_module
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from moex_sentinel.services.trading_admission_ports import (
    AdmissionInstrument,
    AdmissionOrderEstimate,
    AdmissionOrderState,
    AdmissionPosition,
    AdmissionRiskSnapshot,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import BrokerOrderModel, TradeExecutionModel, TradingAutomationModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionEventModel,
    TradingSubmitReservationModel,
)
from moex_sentinel.storage.repositories.trading_admission import TradingAdmissionRepository
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    ReconciliationRequest,
    RevokeRequest,
    RuntimeRegistrationRequest,
    SubmitAuthorizationRequest,
    SubmitPermit,
    UpdateLimitsRequest,
)
from sentinel_contracts.trading_facts import FactEnvelope
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import baseline_command
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession
from trading_automaton.composition import build_worker_recovery
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.services.order_dispatch import OrderDispatchService
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.services.trading_admission import TradingAdmissionService as WorkerAdmissionService
from trading_automaton.services.uncertain_intent_reconciliation import UncertainIntentReconciliationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
from trading_automaton.storage.repository import LocalAutomationRepository

pytestmark = pytest.mark.postgresql

BROKER_ID = UUID("00000000-0000-4000-8000-000000000111")
WORKER_ID = "worker-prod-admission-flow"
WORKER_TOKEN = "a" * 64
CANARY_UID = "flow-canary-uid"
ACCOUNT_ID = "flow-account"
TOKEN = "synthetic-flow-token"  # noqa: S105 - a deterministic credential used only in isolated test PostgreSQL.
_FACT_ADAPTER = TypeAdapter(FactEnvelope)


def _now() -> datetime:
    value = datetime.now(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


class _FreshRisk:
    def __init__(self):
        self.order_states = ()
        self.positions = ()
        self.mutate_during_collect = None
        self.unknown_reasons = ()

    async def collect(self, _connection, *, instrument_uid, **_kwargs):
        if self.mutate_during_collect is not None:
            self.mutate_during_collect()
            self.mutate_during_collect = None
        now = _now()
        return AdmissionRiskSnapshot(
            snapshot_id=str(uuid4()),
            account_id=ACCOUNT_ID,
            capture_started_at=now - timedelta(milliseconds=100),
            captured_at=now,
            valid_until=now + timedelta(seconds=20),
            equity=Decimal("100000"),
            equity_currency="RUB",
            money=Decimal("100000"),
            money_currency="RUB",
            blocked_cash=Decimal(0),
            blocked_cash_currency="RUB",
            money_loaded=True,
            positions=self.positions,
            ordinary_orders=(),
            stop_orders=(),
            order_states=self.order_states,
            operations=(),
            operations_complete=True,
            operations_through=now,
            instrument=(
                None
                if instrument_uid is None
                else AdmissionInstrument(
                    instrument_uid=CANARY_UID,
                    instrument_type="SHARE",
                    currency="RUB",
                    lot_size=10,
                    price_increment=Decimal("0.01"),
                    api_trade_available=True,
                    mark_price=Decimal("10"),
                    mark_captured_at=now,
                )
            ),
            unknown_reasons=self.unknown_reasons,
        )

    async def estimate_limit_order(self, _connection, **_kwargs):
        return AdmissionOrderEstimate(Decimal("100"), Decimal("1"), "RUB", _now())


def _transport(core: TestClient) -> httpx.MockTransport:
    def send(request: httpx.Request) -> httpx.Response:
        response = core.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            headers=dict(request.headers),
            content=request.content,
        )
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


def _settled_reservation_facts(factory, broker_id):
    with factory() as session:
        rows = session.query(TradingSubmitReservationModel).filter_by(user_broker_id=str(broker_id), state="SETTLED")
        return {
            row.idempotency_key: (
                row.state,
                row.request_fingerprint,
                row.reserved_cash,
                row.reserved_position_units,
                row.payload.get("executed_lots"),
                row.payload.get("executed_amount"),
                row.payload.get("executed_commission"),
                row.created_at,
            )
            for row in rows
        }


def _assert_settled_reservations_rebased(factory, broker_id, reconciliation_id):
    with factory() as session:
        rows = session.query(TradingSubmitReservationModel).filter_by(user_broker_id=str(broker_id), state="SETTLED")
        rows = tuple(rows)
    assert rows
    for row in rows:
        assert row.payload["anchor_reconciliation_id"] == str(reconciliation_id)
        assert row.payload["anchor_executed_lots"] == row.payload["executed_lots"]


def _run_worker_expired_permit_flow(
    tmp_path,
    factory,
    worker,
    admission,
    worker_command,
    armed,
    monkeypatch,
    *,
    delivery_mode,
    finalization_mode,
    worker_db_name="admission-worker-outbox.db",
):
    proof_key = uuid4()
    worker_engine = create_worker_engine(f"sqlite:///{tmp_path / worker_db_name}")
    WorkerBase.metadata.create_all(worker_engine)
    local_worker = LocalAutomationRepository(
        sessionmaker(worker_engine, expire_on_commit=False),
        fact_writer=FactOutboxWriter(clock=_now),
        environment="PROD",
    )
    assert not local_worker.begin_run(WORKER_ID)
    assert local_worker.cache_command(worker_command)
    work_item = decision_item(worker_command, intent_id=str(proof_key))
    work_item = work_item.model_copy(
        update={
            "current_price": Decimal("10"),
            "best_bid": Decimal("9.99"),
            "best_ask": Decimal("10"),
            "limit_price": Decimal("10"),
            "strategy_snapshot": {
                "admission_origin": {
                    "runtime_instance_id": str(worker.runtime_instance_id),
                    "generation": worker._registration.generation,
                    "revision": armed.revision,
                    "configuration_fingerprint": armed.configuration_fingerprint,
                    "lot_size": 10,
                    "currency": "RUB",
                    "instrument_uid": CANARY_UID,
                    "fact_instrument_id": str(worker_command.instrument_id),
                }
            },
            "intent": work_item.intent.model_copy(update={"limit_price": Decimal("10")}),
        }
    )
    local_worker.save_decision_batch((work_item,), occurred_at=_now())
    with factory() as session:
        prior_order_count = session.query(BrokerOrderModel).count()
        prior_execution_count = session.query(TradeExecutionModel).count()

    monotonic_ticks = [0.0, 2.0]
    expiring_gate = WorkerAdmissionService(
        worker,
        broker_id=BROKER_ID,
        account_id=ACCOUNT_ID,
        runtime_instance_id=worker.runtime_instance_id,
        configuration_fingerprint=armed.configuration_fingerprint,
        monotonic=lambda: monotonic_ticks.pop(0) if monotonic_ticks else 3.0,
    )
    gate_results = []

    class ObserveExpiryGate:
        async def authorize_submit(self, request):
            result = await expiring_gate.authorize_submit(request)
            gate_results.append(result)
            return result

    sdk_factory_calls = []

    def forbidden_sdk_factory(*_args, **_kwargs):
        sdk_factory_calls.append("constructed")
        raise AssertionError("Expired Core permit must stop before creating the SDK client.")

    connection = admission._connection(str(BROKER_ID))
    sdk = BrokerSdkSession(
        connection.token,
        connection.target,
        environment="PROD",
        access_mode="TRADE",
        account_id=ACCOUNT_ID,
        broker_connection=connection,
        submit_admission=ObserveExpiryGate(),
        client_factory=forbidden_sdk_factory,
        monotonic=lambda: 0.0,
        now=_now,
    )
    request = DispatchRequest(
        idempotency_key=str(proof_key),
        account_id=ACCOUNT_ID,
        instrument_id=CANARY_UID,
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("10"),
        automation_id=str(worker_command.automation_id),
        lot_size=10,
        broker_id=str(BROKER_ID),
        reservation_currency="RUB",
        required_cash=Decimal("101"),
        market_valid_until=_now() + timedelta(seconds=15),
        admission_runtime_instance_id=str(worker.runtime_instance_id),
        admission_generation=worker._registration.generation,
        admission_revision=armed.revision,
        configuration_fingerprint=armed.configuration_fingerprint,
    )
    dispatch = OrderDispatchService(
        local_worker,
        sdk,
        now=_now,
        access_mode="TRADE",
        account_id=ACCOUNT_ID,
    )
    tracker = OrderTrackingService(local_worker, now=_now)

    async def run_dispatch():
        started = asyncio.get_running_loop().create_future()
        dispatch_task = asyncio.create_task(dispatch.dispatch(request, started))
        if finalization_mode == "tracking":
            tracker.track(request.idempotency_key, dispatch_task, request=request)
            await tracker.wait_all()
        return await dispatch_task

    local_result = asyncio.run(run_dispatch())
    assert local_result.status == "CANCELLED"
    assert len(gate_results) == 1
    assert gate_results[0].permit is not None, gate_results[0]
    assert local_result.pre_rpc_abort_proof is not None
    assert local_result.pre_rpc_abort_proof.idempotency_key == proof_key
    assert local_result.pre_rpc_abort_proof.reason == "PERMIT_EXPIRED_BEFORE_POST"
    assert sdk_factory_calls == []

    dispatched_intent = local_worker.get_latest_intent(str(worker_command.automation_id))
    assert dispatched_intent is not None
    if finalization_mode == "recovery":
        assert dispatched_intent.terminal_at is None
    else:
        assert dispatched_intent.terminal_at is not None
        local_worker.finish_run(WORKER_ID)

    local_worker._factory.kw["bind"].dispose()
    reopened_engine = create_worker_engine(f"sqlite:///{tmp_path / worker_db_name}")
    reopened = LocalAutomationRepository(
        sessionmaker(reopened_engine, expire_on_commit=False),
        fact_writer=FactOutboxWriter(clock=_now),
        environment="PROD",
    )
    build_worker_recovery(reopened, WORKER_ID).execute()
    if finalization_mode == "recovery":

        class ForbiddenBrokerReads:
            async def get_order_state(self, *_args, **_kwargs):
                raise AssertionError("Persisted pre-RPC proof recovery must not query broker order state.")

            async def find_by_idempotency_key(self, *_args, **_kwargs):
                raise AssertionError("Persisted pre-RPC proof recovery must not look up a broker order.")

            async def inspect_position(self, *_args, **_kwargs):
                raise AssertionError("Persisted pre-RPC proof recovery must not inspect broker positions.")

            async def inspect_recent_operations(self, *_args, **_kwargs):
                raise AssertionError("Persisted pre-RPC proof recovery must not inspect broker operations.")

        recovered = asyncio.run(
            UncertainIntentReconciliationService(
                reopened,
                ForbiddenBrokerReads(),
                now=_now,
                environment="PROD",
            ).reconcile((worker_command,))
        )
        assert recovered.resolved == 1
        assert recovered.unresolved == 0

    terminal_intent = reopened.get_latest_intent(str(worker_command.automation_id))
    assert terminal_intent is not None
    assert terminal_intent.state == "CANCELLED"
    assert terminal_intent.terminal_at is not None
    terminal_at = terminal_intent.terminal_at

    rows = reopened.ready_fact_outbox(100, now=_now() + timedelta(seconds=2), deadline_ms=0)
    envelopes = _envelopes(rows)
    proof_events = [
        envelope
        for envelope in envelopes
        if envelope.payload.model_dump(mode="json").get("pre_rpc_abort_proof") is not None
    ]
    assert len(proof_events) == 1
    proof_event = proof_events[0]
    proof_payload = proof_event.payload.model_dump(mode="json")["pre_rpc_abort_proof"]
    assert proof_payload == local_result.pre_rpc_abort_proof.model_dump(mode="json")
    terminal_events = [
        envelope
        for envelope in envelopes
        if envelope.payload.model_dump(mode="json").get("to_state") == "CANCELLED"
        and envelope.payload.model_dump(mode="json").get("pre_rpc_abort_proof") is None
    ]
    assert terminal_events

    def publish_replay_and_ack(batch):
        accepted = worker.publish_facts(batch)
        assert accepted.failures == (), accepted.failures
        replay = worker.publish_facts(batch)
        assert replay.failures == (), replay.failures
        assert all(event.event_id in replay.results[0].accepted_event_ids for event in batch)
        acknowledgement = accepted.results[0]
        reopened.acknowledge_fact_outbox(
            str(worker_command.automation_id),
            accepted_through_sequence=acknowledgement.accepted_through_sequence,
            current_revision=acknowledgement.current_revision,
        )

    if delivery_mode == "combined":
        publish_replay_and_ack(envelopes)
    else:
        proof_prefix = [event for event in envelopes if event.sequence_number <= proof_event.sequence_number]
        finalization_suffix = [event for event in envelopes if event.sequence_number > proof_event.sequence_number]
        assert proof_event in proof_prefix
        assert finalization_suffix
        publish_replay_and_ack(proof_prefix)
        publish_replay_and_ack(finalization_suffix)

    with factory() as session:
        confirmed = (
            session.query(TradingSubmitReservationModel)
            .filter_by(user_broker_id=str(BROKER_ID), idempotency_key=str(proof_key))
            .one()
        )
        assert confirmed.state == "CONFIRMED_NOT_SENT"
        assert confirmed.reserved_cash == confirmed.reserved_position_units == 0
        aborted_order = (
            session.query(BrokerOrderModel)
            .filter_by(user_broker_id=str(BROKER_ID), idempotency_key=str(proof_key))
            .one_or_none()
        )
        assert aborted_order is not None
        assert aborted_order.state == "CANCELLED"
        assert aborted_order.external_order_id is None
        assert session.query(BrokerOrderModel).count() == prior_order_count + 1
        assert session.query(TradeExecutionModel).count() == prior_execution_count
    assert reopened.ready_fact_outbox(100, now=_now() + timedelta(seconds=5), deadline_ms=0) == []
    diagnostics = reopened.outbox_diagnostics()
    assert diagnostics.pending_count == 0
    assert diagnostics.failed_count == 0
    persisted_after_ack = reopened.get_latest_intent(str(worker_command.automation_id))
    assert persisted_after_ack is not None
    assert persisted_after_ack.terminal_at == terminal_at

    consumed_request = SubmitAuthorizationRequest(
        broker_id=BROKER_ID,
        account_id=ACCOUNT_ID,
        automation_id=worker_command.automation_id,
        idempotency_key=proof_key,
        generation=worker._registration.generation,
        revision=armed.revision,
        configuration_fingerprint=armed.configuration_fingerprint,
        instrument_uid=CANARY_UID,
        lot_size=10,
        currency="RUB",
        side="BUY",
        quantity_lots=1,
        limit_price=Decimal("10"),
        market_valid_until=_now() + timedelta(seconds=15),
        submit_deadline=_now() + timedelta(seconds=1),
    )
    for _ in range(2):
        consumed = worker.authorize_submit(consumed_request)
        assert consumed.result == "DENIED"
        assert consumed.reason.value == "ALREADY_CONSUMED"
        assert consumed.outcome_uncertain
    with pytest.raises(CoreOperationError) as consumed_retry:
        asyncio.run(expiring_gate.authorize_submit(request))
    assert consumed_retry.value.retryable
    reopened_engine.dispose()
    worker_engine.dispose()


@pytest.mark.parametrize("delivery_mode", ["combined", "split"])
@pytest.mark.parametrize("finalization_mode", ["tracking", "recovery"])
def test_prod_trade_settings_http_enrollment_transfer_recovery_and_next_submit(
    isolated_postgresql_database_url, tmp_path, monkeypatch, delivery_mode, finalization_mode
):
    for key, value in {
        "APPLICATION_ENVIRONMENT": "PROD",
        "BROKER_ACCESS_MODE": "READ_ONLY",
        "AUTH_ALLOWED_ORIGIN": "https://operator.example.test",
        "AUTH_SESSION_COOKIE_NAME": "__Host-flow-core-session",
        "INTERNAL_WORKER_ID": WORKER_ID,
        "INTERNAL_WORKER_BROKER_ID": str(BROKER_ID),
        "INTERNAL_WORKER_TOKEN": WORKER_TOKEN,
    }.items():
        monkeypatch.setenv(key, value)

    automation_id = uuid4()
    worker_command = baseline_command().model_copy(
        update={
            "user_broker_id": BROKER_ID,
            "broker_id": BROKER_ID,
            "account_id": ACCOUNT_ID,
            "automation_id": automation_id,
            "external_instrument_id": CANARY_UID,
        }
    )
    instrument_id = str(worker_command.instrument_id)
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        broker = user_broker_model(str(BROKER_ID), ACCOUNT_ID)
        broker.environment = "PROD"
        broker.fqdn = "invest-public-api.tbank.ru:443"
        session.add(broker)
        session.flush()
        instrument = instrument_model(instrument_id, str(BROKER_ID))
        instrument.ticker = "FLOW"
        instrument.name = "Admission flow"
        instrument.external_instrument_id = CANARY_UID
        session.add(instrument)
        session.flush()
        session.add(
            automation_model(
                str(automation_id),
                user_broker_id=str(BROKER_ID),
                instrument_id=instrument_id,
                state="IN_QUEUE",
            )
        )

    old_runtime = uuid4()
    old_secret = "b" * 64
    old_request = RuntimeRegistrationRequest(
        worker_id=WORKER_ID,
        runtime_instance_id=old_runtime,
        broker_id=BROKER_ID,
        application_environment="PROD",
        access_mode="READ_ONLY",
        strategy_enabled=False,
    )
    readonly_app = app_module.create_app(database_url=isolated_postgresql_database_url, test_auth_bypass=True)
    with TestClient(readonly_app) as readonly_core:
        readonly_http = httpx.Client(transport=_transport(readonly_core), base_url="http://core.test")
        old_response = readonly_http.post(
            "/internal/automaton/runtime-instances",
            json=old_request.model_dump(mode="json"),
            headers={
                "Authorization": f"Bearer {WORKER_TOKEN}",
                "X-Runtime-Instance-ID": str(old_runtime),
                "X-Runtime-Secret": old_secret,
            },
        )
        assert old_response.status_code == 200, old_response.text
        assert old_response.json()["registration_status"] == "OWNER"
        readonly_http.close()

    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    app = app_module.create_app(database_url=isolated_postgresql_database_url, test_auth_bypass=True)
    activation_engine = None
    with TestClient(app) as core:
        risk = _FreshRisk()
        admission = core.app.state.usecases.trading_admission
        admission._risk = risk
        http = httpx.Client(transport=_transport(core), base_url="http://core.test")
        try:
            worker_http = httpx.Client(transport=_transport(core), base_url="http://core.test")
            worker = CoreClient(
                worker_http,
                application_environment="PROD",
                access_mode="TRADE",
                worker_id=WORKER_ID,
                broker_id=BROKER_ID,
                worker_token=WORKER_TOKEN,
                strategy_enabled=True,
            )
            pending = worker.register_runtime(
                RuntimeRegistrationRequest(
                    worker_id=WORKER_ID,
                    runtime_instance_id=worker.runtime_instance_id,
                    broker_id=BROKER_ID,
                    application_environment="PROD",
                    access_mode="TRADE",
                    strategy_enabled=True,
                )
            )
            assert pending.registration_status.value == "PENDING"

            before = admission.view(str(BROKER_ID))
            now = _now()
            transferred = admission.owner_stopped(
                str(BROKER_ID),
                OwnerStoppedRequest(
                    expected_revision=before.revision,
                    old_runtime_instance_id=old_runtime,
                    old_generation=before.owner_generation,
                    candidate_runtime_instance_id=worker.runtime_instance_id,
                    stopped_at=now,
                    auto_restart_disabled=True,
                    evidence_ref="test://operator-confirmed-stop",
                ),
                actor="test-operator",
            )
            assert transferred.owner_runtime_instance_id == worker.runtime_instance_id
            assert transferred.recovery_only

            worker.validate_runtime()
            assert worker._registration.registration_status.value == "OWNER"
            assert worker._registration.generation == transferred.owner_generation
            claimed_commands = worker.claim_commands(WORKER_ID, limit=10)
            assert len(claimed_commands) == 1
            claimed_command = claimed_commands[0]
            assert claimed_command.state is AutomationState.IN_QUEUE
            assert claimed_command.user_broker_id == BROKER_ID
            assert claimed_command.broker_id == BROKER_ID
            assert claimed_command.account_id == ACCOUNT_ID

            activation_engine = create_worker_engine(f"sqlite:///{tmp_path / 'activation-worker.db'}")
            WorkerBase.metadata.create_all(activation_engine)
            activation_worker = LocalAutomationRepository(
                sessionmaker(activation_engine, expire_on_commit=False),
                fact_writer=FactOutboxWriter(clock=_now),
                environment="PROD",
            )
            assert activation_worker.cache_command(claimed_command)
            assert activation_worker.transition_state(
                automation_id=str(automation_id),
                state=AutomationState.IN_WORK,
                safe_message="Synthetic worker activation",
                occurred_at=_now(),
            )
            synchronization = FactSynchronizationService(
                activation_worker,
                worker,
                now=_now,
                sleep=lambda _seconds: None,
                deadline_ms=0,
            )
            assert synchronization.flush_outbox()
            with factory() as session:
                activated = session.get_one(TradingAutomationModel, str(automation_id))
                assert (activated.state, activated.revision, activated.last_sequence_number) == ("IN_WORK", 2, 1)
            worker_command = claimed_command.model_copy(
                update={
                    "state": AutomationState.IN_WORK,
                    "revision": 2,
                    "last_sequence_number": 1,
                }
            )

            today = _now().astimezone(ZoneInfo("Europe/Moscow")).date()
            baseline = asyncio.run(
                admission.capture_baseline(
                    str(BROKER_ID), CaptureBaselineRequest(day=today, accept_policy=True), accepted_by="test-operator"
                )
            )
            view = admission.view(str(BROKER_ID))
            reconciliation = asyncio.run(
                admission.reconcile(
                    str(BROKER_ID),
                    ReconciliationRequest(expected_revision=view.revision, canary_uid=CANARY_UID),
                    runtime_instance_id=worker.runtime_instance_id,
                    generation=worker._registration.generation,
                )
            )
            assert reconciliation.can_arm
            limits = AdmissionLimits(
                canary_uid=CANARY_UID,
                currency="RUB",
                max_order_notional=Decimal("1000"),
                exposure_scope="CANARY_UID_GROSS",
                max_exposure=Decimal("10000"),
                max_outstanding_orders=2,
                max_daily_loss=Decimal("1000"),
            )
            armed = admission.arm(
                str(BROKER_ID),
                ArmRequest(
                    expected_revision=admission.view(str(BROKER_ID)).revision,
                    runtime_instance_id=worker.runtime_instance_id,
                    baseline_id=baseline.baseline_id,
                    reconciliation_id=reconciliation.reconciliation_id,
                    limits=limits,
                    configuration_fingerprint=worker.trading_admission(str(BROKER_ID)).configuration_fingerprint,
                    expires_at=_now() + timedelta(minutes=2),
                ),
                actor="test-operator",
            )
            worker_gate = WorkerAdmissionService(
                worker,
                broker_id=BROKER_ID,
                account_id=ACCOUNT_ID,
                runtime_instance_id=worker.runtime_instance_id,
                configuration_fingerprint=armed.configuration_fingerprint,
            )
            assert asyncio.run(worker_gate.prepare(CANARY_UID)).effective_can_submit

            sdk_factory_calls = []
            stale_gate_results = []

            class ObserveStaleGate:
                async def authorize_submit(self, request):
                    result = await worker_gate.authorize_submit(request)
                    stale_gate_results.append(result)
                    return result

            def unexpected_sdk_factory(*_args, **_kwargs):
                sdk_factory_calls.append("constructed")
                raise AssertionError("Stale Worker lot metadata must deny before SDK construction.")

            connection = admission._connection(str(BROKER_ID))
            stale_sdk = BrokerSdkSession(
                connection.token,
                connection.target,
                environment="PROD",
                access_mode="TRADE",
                account_id=ACCOUNT_ID,
                broker_connection=connection,
                submit_admission=ObserveStaleGate(),
                client_factory=unexpected_sdk_factory,
                now=_now,
            )
            stale_lot_request = DispatchRequest(
                idempotency_key=str(uuid4()),
                account_id=ACCOUNT_ID,
                instrument_id=CANARY_UID,
                side=OrderSide.BUY,
                quantity_lots=1,
                limit_price=Decimal("10"),
                automation_id=str(automation_id),
                lot_size=1,
                broker_id=str(BROKER_ID),
                reservation_currency="RUB",
                required_cash=Decimal("11"),
                market_valid_until=_now() + timedelta(seconds=15),
                admission_runtime_instance_id=str(worker.runtime_instance_id),
                admission_generation=worker._registration.generation,
                admission_revision=armed.revision,
                configuration_fingerprint=armed.configuration_fingerprint,
            )
            stale_result = asyncio.run(stale_sdk.dispatch_limit_order(stale_lot_request))
            assert stale_result.status == "CANCELLED"
            assert stale_result.pre_rpc_abort_proof is None
            assert len(stale_gate_results) == 1
            assert stale_gate_results[0].denial.reason.value == "DATA_UNKNOWN"
            assert stale_gate_results[0].permit is None
            assert sdk_factory_calls == []
            with factory() as session:
                assert session.query(TradingSubmitReservationModel).count() == 0
                assert session.query(BrokerOrderModel).count() == 0
                assert session.query(TradeExecutionModel).count() == 0

            first_key = uuid4()

            def submit(key):
                return asyncio.run(
                    worker_gate.authorize_submit(
                        DispatchRequest(
                            idempotency_key=str(key),
                            account_id=ACCOUNT_ID,
                            instrument_id=CANARY_UID,
                            side=OrderSide.BUY,
                            quantity_lots=1,
                            limit_price=Decimal("10"),
                            automation_id=str(automation_id),
                            lot_size=10,
                            broker_id=str(BROKER_ID),
                            reservation_currency="RUB",
                            required_cash=Decimal("101"),
                            market_valid_until=_now() + timedelta(seconds=15),
                            admission_runtime_instance_id=str(worker.runtime_instance_id),
                            admission_generation=worker._registration.generation,
                            admission_revision=armed.revision,
                            configuration_fingerprint=armed.configuration_fingerprint,
                        )
                    )
                )

            first = submit(first_key)
            assert isinstance(first.permit, SubmitPermit), first
            assert asyncio.run(worker_gate.prepare(CANARY_UID)).effective_can_submit
            risk.order_states = (
                AdmissionOrderState(
                    request_id=str(first_key),
                    broker_order_id="flow-filled-order",
                    instrument_uid=CANARY_UID,
                    side=OrderSide.BUY,
                    requested_lots=1,
                    executed_lots=1,
                    executed_amount=Decimal("100"),
                    executed_commission=Decimal("1"),
                    currency="RUB",
                    status="FILLED",
                    attributable=True,
                    unknown_reason=None,
                ),
            )
            risk.positions = (
                AdmissionPosition(
                    instrument_uid=CANARY_UID,
                    quantity_units=Decimal("10"),
                    blocked_units=Decimal(0),
                    mark_price=Decimal("10"),
                    currency="RUB",
                    lot_size=10,
                    mark_captured_at=_now(),
                ),
            )
            second_key = uuid4()
            second = submit(second_key)
            assert isinstance(second.permit, SubmitPermit), second
            with factory() as session:
                reservations = session.query(TradingSubmitReservationModel).all()
                assert len(reservations) == 2
                settled_first = next(item for item in reservations if item.idempotency_key == str(first_key))
                assert settled_first.state == "SETTLED"

            # Incomplete submit evidence must not grant or mutate a reservation.
            risk.unknown_reasons = ("synthetic incomplete snapshot",)
            unknown_key = uuid4()
            unknown = submit(unknown_key)
            assert unknown.permit is None
            assert unknown.denial.reason.value == "DATA_UNKNOWN"
            with factory() as session:
                reservations = session.query(TradingSubmitReservationModel).all()
                assert len(reservations) == 2
                unresolved_second = next(item for item in reservations if item.idempotency_key == str(second_key))
                assert unresolved_second.state == "IN_FLIGHT"
                assert unresolved_second.reserved_cash == Decimal("101")

            # Fresh terminal broker evidence settles the unresolved R2 through the
            # normal worker authorization and pre-RPC finalization path.
            risk.unknown_reasons = ()
            risk.positions = (
                AdmissionPosition(
                    instrument_uid=CANARY_UID,
                    quantity_units=Decimal("20"),
                    blocked_units=Decimal(0),
                    mark_price=Decimal("10"),
                    currency="RUB",
                    lot_size=10,
                    mark_captured_at=_now(),
                ),
            )
            risk.order_states = (
                AdmissionOrderState(
                    request_id=str(second_key),
                    broker_order_id="flow-second-filled-order",
                    instrument_uid=CANARY_UID,
                    side=OrderSide.BUY,
                    requested_lots=1,
                    executed_lots=1,
                    executed_amount=Decimal("100"),
                    executed_commission=Decimal("1"),
                    currency="RUB",
                    status="FILLED",
                    attributable=True,
                    unknown_reason=None,
                ),
            )
            asyncio.run(worker_gate.prepare(CANARY_UID))
            _run_worker_expired_permit_flow(
                tmp_path,
                factory,
                worker,
                admission,
                worker_command,
                armed,
                monkeypatch,
                delivery_mode=delivery_mode,
                finalization_mode=finalization_mode,
                worker_db_name="admission-worker-terminal-proof.db",
            )
            settled_facts_before_rotation = _settled_reservation_facts(factory, BROKER_ID)
            with factory() as session:
                settled_second = (
                    session.query(TradingSubmitReservationModel).filter_by(idempotency_key=str(second_key)).one()
                )
                assert settled_second.state == "SETTLED"
                assert settled_second.reserved_cash == settled_second.reserved_position_units == 0

            risk.positions = (
                AdmissionPosition(
                    instrument_uid=CANARY_UID,
                    quantity_units=Decimal("20"),
                    blocked_units=Decimal(0),
                    mark_price=Decimal("10"),
                    currency="RUB",
                    lot_size=10,
                    mark_captured_at=_now(),
                ),
            )
            before_recovery = admission.view(str(BROKER_ID))
            recovered = asyncio.run(
                admission.reconcile(
                    str(BROKER_ID),
                    ReconciliationRequest(expected_revision=before_recovery.revision, canary_uid=CANARY_UID),
                    runtime_instance_id=worker.runtime_instance_id,
                    generation=worker._registration.generation,
                )
            )
            after_recovery = admission.view(str(BROKER_ID))
            assert recovered.can_arm
            assert after_recovery.reconciliation.reconciliation_id == recovered.reconciliation_id
            _assert_settled_reservations_rebased(factory, BROKER_ID, recovered.reconciliation_id)
            assert _settled_reservation_facts(factory, BROKER_ID) == settled_facts_before_rotation

            risk.order_states = ()
            before_clear = admission.view(str(BROKER_ID))
            cleared = asyncio.run(
                admission.reconcile(
                    str(BROKER_ID),
                    ReconciliationRequest(expected_revision=before_clear.revision, canary_uid=CANARY_UID),
                    runtime_instance_id=worker.runtime_instance_id,
                    generation=worker._registration.generation,
                )
            )
            assert cleared.can_arm
            _assert_settled_reservations_rebased(factory, BROKER_ID, cleared.reconciliation_id)
            assert _settled_reservation_facts(factory, BROKER_ID) == settled_facts_before_rotation
            ready = admission.view(str(BROKER_ID))
            assert not ready.recovery_only
            assert ready.unresolved_reservation_count == 0
            armed_again = admission.arm(
                str(BROKER_ID),
                ArmRequest(
                    expected_revision=ready.revision,
                    runtime_instance_id=worker.runtime_instance_id,
                    baseline_id=baseline.baseline_id,
                    reconciliation_id=cleared.reconciliation_id,
                    limits=limits,
                    configuration_fingerprint=ready.configuration_fingerprint,
                    expires_at=_now() + timedelta(minutes=2),
                ),
                actor="test-operator",
            )
            risk.positions = (
                AdmissionPosition(
                    instrument_uid=CANARY_UID,
                    quantity_units=Decimal("20"),
                    blocked_units=Decimal(0),
                    mark_price=Decimal("10"),
                    currency="RUB",
                    lot_size=10,
                    mark_captured_at=_now(),
                ),
            )
            with factory() as session:
                latest_automation = session.get_one(TradingAutomationModel, str(automation_id))
                worker_command = worker_command.model_copy(
                    update={
                        "state": AutomationState(latest_automation.state),
                        "revision": latest_automation.revision,
                        "last_sequence_number": latest_automation.last_sequence_number,
                    }
                )
            _run_worker_expired_permit_flow(
                tmp_path,
                factory,
                worker,
                admission,
                worker_command,
                armed_again,
                monkeypatch,
                delivery_mode=delivery_mode,
                finalization_mode=finalization_mode,
            )
            assert _settled_reservation_facts(factory, BROKER_ID) == settled_facts_before_rotation
        finally:
            http.close()
            if "worker_http" in locals():
                worker_http.close()
            if activation_engine is not None:
                activation_engine.dispose()
    engine.dispose()


def test_test_scoped_repository_rejects_prod_operator_mutations_transactionally(isolated_postgresql_database_url):
    broker_id = str(uuid4())
    engine = create_database_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        broker = user_broker_model(broker_id, "prod-cross-contour-account")
        broker.environment = "PROD"
        broker.fqdn = "invest-public-api.tbank.ru:443"
        session.add(broker)
        session.flush()
        session.add(TradingAccountAdmissionModel(user_broker_id=broker_id, revision=1, limits=None))

    repository = TradingAdmissionRepository(factory, application_environment="TEST")
    limits = AdmissionLimits(
        currency="RUB",
        max_order_notional=Decimal("1000"),
        exposure_scope="ACCOUNT_GROSS",
        max_exposure=Decimal("5000"),
        max_outstanding_orders=3,
        max_daily_loss=Decimal("250"),
    )
    with pytest.raises(AdmissionConflictError):
        repository.update_limits(
            broker_id,
            UpdateLimitsRequest(expected_revision=1, limits=limits),
            actor="test-operator",
            now=_now(),
        )
    with pytest.raises(AdmissionConflictError):
        repository.revoke(
            RevokeRequest(expected_revision=1, reason="synthetic cross-contour test"),
            broker_id=broker_id,
            now=_now(),
            actor="test-operator",
        )
    with pytest.raises(ValueError, match="Trading admission has not been registered"):
        repository.get_view(broker_id, now=_now())
    with pytest.raises(ValueError, match="Pending runtime scope is unavailable"):
        repository.pending_runtime_candidate(broker_id, "worker", _now() - timedelta(minutes=1))

    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, broker_id)
        assert admission is not None
        assert admission.revision == 1
        assert admission.limits is None
        assert admission.admission_status == "DISARMED"
        assert session.query(TradingAdmissionEventModel).filter_by(user_broker_id=broker_id).count() == 0
    engine.dispose()
