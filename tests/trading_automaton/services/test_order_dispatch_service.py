import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition, BrokerRecoveryOperation, OrderSide
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_admission import (
    PreRpcAbortProof,
    SubmitAuthorizationRequest,
    submit_request_fingerprint,
)
from sentinel_contracts.trading_facts import FactKind
from tests.trading_automaton.command_factory import command as baseline_command
from tests.trading_automaton.command_factory import decision_item
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.services.order_dispatch import (
    DispatchRequest,
    OrderDispatchService,
)
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.services.uncertain_intent_reconciliation import (
    UncertainIntentReconciliationService,
)
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Broker:
    def __init__(
        self,
        release: asyncio.Event,
        *,
        error: Exception | None = None,
        reconciled: BrokerOrderState | None = None,
    ) -> None:
        self.release = release
        self.error = error
        self.called = asyncio.Event()
        self.reconciled = reconciled
        self.reconciliation_calls = []
        self.position_calls = []
        self.operation_calls = []

    async def dispatch_limit_order(self, request):
        self.dispatch_request = request
        self.called.set()
        await self.release.wait()
        if self.error:
            raise self.error
        return BrokerOrderState(
            "order-1",
            request.idempotency_key,
            "ACCEPTED",
            1,
            0,
            Decimal("1000"),
            Decimal(),
            Decimal("1"),
            Decimal(),
            "RUB",
        )

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        self.reconciliation_calls.append((account_id, idempotency_key))
        return self.reconciled

    async def inspect_position(self, account_id, instrument_id):
        self.position_calls.append((account_id, instrument_id))
        return BrokerPosition(instrument_id, Decimal(2), Decimal(100), Decimal(101), "RUB")

    async def inspect_recent_operations(self, account_id, instrument_id, limit):
        self.operation_calls.append((account_id, instrument_id, limit))
        return (
            BrokerRecoveryOperation("operation", OrderSide.BUY, True, NOW, Decimal(1), Decimal(100), Decimal(1), "RUB"),
        )


class Repository:
    def __init__(self) -> None:
        self.updates = []
        self.holds = []

    def update_intent(self, idempotency_key, **values):
        self.updates.append((idempotency_key, values))

    def hold_active(self, reason, automation_id=None):
        self.holds.append((reason, automation_id))


def request() -> DispatchRequest:
    return DispatchRequest("intent-1", "account-1", "instrument-1", OrderSide.BUY, 1, Decimal("100"))


@pytest.mark.parametrize("delay_in_audit", [False, True])
def test_expired_market_cancels_persisted_intent_without_broker_call(delay_in_audit):
    async def scenario():
        clock = [NOW if delay_in_audit else NOW + timedelta(seconds=3)]

        class Audit:
            def record_order_stage(self, **values):
                clock[0] = NOW + timedelta(seconds=3)

        release = asyncio.Event()
        release.set()
        broker = Broker(release)
        repository = Repository()
        service = OrderDispatchService(repository, broker, now=lambda: clock[0], audit=Audit(), access_mode="TRADE")
        started = asyncio.get_running_loop().create_future()
        value = request().model_copy(update={"market_valid_until": NOW + timedelta(seconds=2), "process_id": "process"})
        result = await service.dispatch(value, started)
        assert result.status == "CANCELLED"
        assert result.executed_lots == 0
        assert not broker.called.is_set()
        assert not broker.reconciliation_calls
        assert repository.updates[-1][1]["state"] == "CANCELLED"
        assert started.done()

    asyncio.run(scenario())


def test_expired_prod_retry_with_ambiguous_consumed_key_stays_uncertain_without_false_cancel():
    async def scenario():
        release = asyncio.Event()
        release.set()
        broker = Broker(release, error=CoreOperationError(retryable=True))
        repository = Repository()
        service = OrderDispatchService(repository, broker, now=lambda: NOW, access_mode="TRADE")
        value = request().model_copy(
            update={
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
                "automation_id": "00000000-0000-4000-8000-000000000002",
                "broker_id": "00000000-0000-4000-8000-000000000003",
                "configuration_fingerprint": "a" * 64,
                "market_valid_until": NOW - timedelta(seconds=1),
            }
        )

        with pytest.raises(CoreOperationError):
            await service.dispatch(value, asyncio.get_running_loop().create_future())

        return broker, repository

    broker, repository = asyncio.run(scenario())

    assert broker.called.is_set()  # The PROD boundary performs Core's consumed-key lookup.
    assert broker.reconciliation_calls == [("account-1", "00000000-0000-4000-8000-000000000001")]
    assert [update[1]["state"] for update in repository.updates] == ["SUBMITTING", "UNCERTAIN"]


def test_expired_prod_lost_grant_ack_holds_and_keeps_buy_reservation(tmp_path):
    class AmbiguousAdmissionBroker(Broker):
        def __init__(self):
            super().__init__(asyncio.Event(), error=CoreOperationError(retryable=True))
            self.sdk_post_calls = 0

        async def dispatch_limit_order(self, request):
            self.dispatch_request = request
            self.called.set()
            raise CoreOperationError(retryable=True)  # Core may have consumed the same request key.

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.reconciliation_calls.append((account_id, idempotency_key))

    engine = create_worker_engine(f"sqlite:///{tmp_path / 'expired-lost-ack.db'}")
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(
        sessionmaker(engine, expire_on_commit=False),
        fact_writer=FactOutboxWriter(clock=lambda: NOW),
        environment="PROD",
    )
    command_value = baseline_command()
    repo.cache_command(command_value)
    item = decision_item(command_value)
    repo.save_decision_batch((item,), occurred_at=NOW)
    prod_request = request().model_copy(
        update={
            "idempotency_key": item.intent.idempotency_key,
            "account_id": item.account_id,
            "automation_id": item.automation_id,
            "broker_id": item.broker_id,
            "instrument_id": item.instrument_id,
            "lot_size": item.lot_size,
            "required_cash": item.intent.limit_price * item.lot_size * item.intent.quantity_lots
            + item.estimated_commission,
            "reservation_currency": item.currency,
            "configuration_fingerprint": "a" * 64,
            "market_valid_until": NOW - timedelta(seconds=1),
        }
    )
    broker = AmbiguousAdmissionBroker()
    service = OrderDispatchService(repo, broker, now=lambda: NOW, access_mode="TRADE")

    async def scenario():
        with pytest.raises(CoreOperationError):
            await service.dispatch(prod_request, asyncio.get_running_loop().create_future())

    try:
        asyncio.run(scenario())
        active = repo.get_active_intent(item.automation_id)
        reservations = repo.list_active_buy_intent_reservations()

        assert active is not None
        assert active.state == "UNCERTAIN"
        assert repo.get_state(item.automation_id).state == "HOLD"
        assert len(reservations) == 1
        assert reservations[0].intent_id == item.intent.idempotency_key
        assert broker.sdk_post_calls == 0
        assert broker.reconciliation_calls == [(item.account_id, item.intent.idempotency_key)]
    finally:
        engine.dispose()


def test_pre_rpc_cancel_dispatch_recovery_and_tracking_replay_keep_zero_fee(tmp_path):
    command_value = baseline_command()
    runtime_id = UUID("00000000-0000-4000-8000-000000000731")
    generation = 2
    revision = 1
    configuration_fingerprint = "a" * 64
    origin = {
        "runtime_instance_id": str(runtime_id),
        "generation": generation,
        "revision": revision,
        "configuration_fingerprint": configuration_fingerprint,
        "lot_size": command_value.lot_size,
        "currency": command_value.currency,
        "instrument_uid": command_value.external_instrument_id,
        "fact_instrument_id": str(command_value.instrument_id),
    }
    item = decision_item(command_value).model_copy(update={"strategy_snapshot": {"admission_origin": origin}})
    database_url = f"sqlite:///{tmp_path / 'dispatch-proof-recovery-replay.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(
        sessionmaker(engine, expire_on_commit=False),
        fact_writer=FactOutboxWriter(clock=lambda: NOW),
        environment="PROD",
    )
    repo.cache_command(command_value)
    batch = repo.save_decision_batch((item,), occurred_at=NOW)
    intent_id = batch.intents[0].idempotency_key
    authorization = SubmitAuthorizationRequest(
        broker_id=command_value.broker_id,
        account_id=command_value.account_id,
        automation_id=command_value.automation_id,
        idempotency_key=UUID(intent_id),
        generation=generation,
        revision=revision,
        configuration_fingerprint=configuration_fingerprint,
        instrument_uid=command_value.external_instrument_id,
        lot_size=command_value.lot_size,
        currency=command_value.currency,
        side="BUY",
        quantity_lots=1,
        limit_price=Decimal("100"),
        market_valid_until=NOW + timedelta(seconds=10),
        submit_deadline=NOW + timedelta(seconds=1),
    )
    proof = PreRpcAbortProof(
        permit_id=UUID("00000000-0000-4000-8000-000000000732"),
        idempotency_key=UUID(intent_id),
        request_fingerprint=submit_request_fingerprint(authorization),
        runtime_instance_id=runtime_id,
        generation=generation,
        configuration_fingerprint=configuration_fingerprint,
        aborted_at=NOW,
        reason="PERMIT_EXPIRED_BEFORE_POST",
    )
    response = BrokerOrderState(
        broker_order_id="",
        idempotency_key=intent_id,
        status="CANCELLED",
        requested_lots=1,
        executed_lots=0,
        requested_amount=Decimal("1000"),
        executed_amount=Decimal(),
        estimated_commission=Decimal(),
        executed_commission=Decimal(),
        currency="RUB",
        pre_rpc_abort_proof=proof,
    )

    class ProofBroker(Broker):
        async def dispatch_limit_order(self, request_value):
            self.dispatch_request = request_value
            self.called.set()
            return response

    audit_started = Event()
    release_audit = Event()

    class PausedResultAudit:
        def record_order_stage(self, **values):
            if values["stage"] == BusinessAuditStage.BROKER_ORDER_DISPATCH_RESULT:
                audit_started.set()
                if not release_audit.wait(timeout=5):
                    raise TimeoutError("Result audit pause was not released")

    broker = ProofBroker(asyncio.Event())
    dispatch = OrderDispatchService(
        repo,
        broker,
        now=lambda: NOW,
        audit=PausedResultAudit(),
        access_mode="TRADE",
    )
    prod_request = request().model_copy(
        update={
            "idempotency_key": intent_id,
            "account_id": item.account_id,
            "automation_id": item.automation_id,
            "broker_id": item.broker_id,
            "instrument_id": item.instrument_id,
            "lot_size": item.lot_size,
            "required_cash": item.average_price * item.lot_size + item.estimated_commission,
            "reservation_currency": item.currency,
            "configuration_fingerprint": configuration_fingerprint,
            "market_valid_until": NOW + timedelta(seconds=10),
            "process_id": item.process_id,
        }
    )

    class NoBrokerReads(Broker):
        async def find_by_idempotency_key(self, account_id, idempotency_key):
            raise AssertionError("A durable pre-RPC proof should bypass broker lookup")

        async def inspect_position(self, account_id, instrument_id):
            raise AssertionError("A durable pre-RPC proof should bypass position inspection")

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            raise AssertionError("PROD proof recovery must not infer from recent operations")

    async def scenario():
        started = asyncio.get_running_loop().create_future()
        dispatch_task = asyncio.create_task(dispatch.dispatch(prod_request, started))
        audit_reached = await asyncio.to_thread(audit_started.wait, 5)
        assert audit_reached

        persisted = repo.get_active_intent(item.automation_id)
        assert persisted is not None
        assert persisted.estimated_commission == Decimal()
        assert persisted.strategy_snapshot["_pre_rpc_abort_proof"] == proof.model_dump(mode="json")

        recovery = UncertainIntentReconciliationService(
            repo,
            NoBrokerReads(asyncio.Event()),
            now=lambda: NOW + timedelta(seconds=2),
            environment="PROD",
        )
        result = await recovery.reconcile((command_value,))
        assert result.resolved == 1
        terminal = repo.get_latest_intent(item.automation_id)
        assert terminal is not None
        assert terminal.state == "CANCELLED"
        assert terminal.executed_lots == 0
        assert terminal.estimated_commission == Decimal()
        facts_after_recovery = repo.ready_fact_outbox(20, now=NOW + timedelta(seconds=3), deadline_ms=0)
        proof_facts = [fact for fact in facts_after_recovery if fact.payload.get("pre_rpc_abort_proof") is not None]
        order_state_facts = [
            fact for fact in facts_after_recovery if fact.fact_kind == FactKind.BROKER_ORDER_STATE_CHANGED.value
        ]
        assert len(proof_facts) == 1
        assert order_state_facts[-1].payload.get("pre_rpc_abort_proof") is None

        release_audit.set()
        assert await dispatch_task == response
        tracking = OrderTrackingService(repo, now=lambda: NOW + timedelta(seconds=4))
        tracking.track(intent_id, dispatch_task, request=prod_request)
        await tracking.wait_all()
        facts_after_tracking = repo.ready_fact_outbox(20, now=NOW + timedelta(seconds=5), deadline_ms=0)

        assert repo.get_state(item.automation_id).state != "HOLD"
        assert facts_after_tracking == facts_after_recovery
        assert repo.get_active_intent(item.automation_id) is None

    try:
        asyncio.run(scenario())
    finally:
        release_audit.set()
        engine.dispose()


def test_marks_sdk_start_before_waiting_for_broker_response() -> None:
    async def scenario():
        release = asyncio.Event()
        broker = Broker(release)
        started = asyncio.get_running_loop().create_future()
        repository = Repository()
        service = OrderDispatchService(repository, broker, now=lambda: NOW, access_mode="TRADE")
        task = asyncio.create_task(service.dispatch(request(), started))
        await broker.called.wait()
        assert started.result() == NOW
        assert repository.updates == [
            (
                "intent-1",
                {
                    "state": "SUBMITTING",
                    "occurred_at": NOW,
                    "dispatch_started_at": NOW,
                    "process_id": None,
                },
            )
        ]
        assert not task.done()
        release.set()
        result = await task
        return result, repository

    result, repository = asyncio.run(scenario())

    assert result.status == "ACCEPTED"
    assert repository.updates[-1] == (
        "intent-1",
        {
            "state": "ACCEPTED",
            "occurred_at": NOW,
            "broker_order_id": "order-1",
            "broker_responded_at": NOW,
            "process_id": None,
        },
    )


def test_ambiguous_sdk_failure_marks_intent_uncertain() -> None:
    async def scenario():
        release = asyncio.Event()
        release.set()
        repository = Repository()
        broker = Broker(release, error=TimeoutError("synthetic timeout"))
        started = asyncio.get_running_loop().create_future()
        service = OrderDispatchService(repository, broker, now=lambda: NOW, access_mode="TRADE")
        with pytest.raises(TimeoutError):
            await service.dispatch(request(), started)
        return repository

    repository = asyncio.run(scenario())

    assert repository.updates[-1] == (
        "intent-1",
        {"state": "UNCERTAIN", "occurred_at": NOW, "process_id": None},
    )


def test_sdk_failure_recovers_order_by_idempotency_key() -> None:
    async def scenario():
        release = asyncio.Event()
        release.set()
        reconciled = BrokerOrderState(
            "order-1",
            "intent-1",
            "FILLED",
            1,
            1,
            Decimal("100"),
            Decimal("100"),
            Decimal("1"),
            Decimal("1"),
            "RUB",
            Decimal("100"),
            NOW,
        )
        repository = Repository()
        broker = Broker(release, error=TimeoutError("synthetic timeout"), reconciled=reconciled)
        service = OrderDispatchService(repository, broker, now=lambda: NOW, access_mode="TRADE")
        started = asyncio.get_running_loop().create_future()
        result = await service.dispatch(request(), started)
        return result, repository, broker

    result, repository, broker = asyncio.run(scenario())

    assert result.status == "FILLED"
    assert repository.updates[-1] == (
        "intent-1",
        {
            "state": "FILLED",
            "occurred_at": NOW,
            "broker_order_id": "order-1",
            "broker_responded_at": NOW,
            "process_id": None,
        },
    )
    assert broker.reconciliation_calls == [("account-1", "intent-1")]
    assert broker.position_calls == [("account-1", "instrument-1")]
    assert broker.operation_calls == [("account-1", "instrument-1", 20)]


def test_prod_ambiguous_submit_uses_only_request_key_lookup_and_short_deadline():
    async def scenario():
        release = asyncio.Event()
        release.set()
        broker = Broker(release, error=TimeoutError("uncertain"))
        service = OrderDispatchService(Repository(), broker, now=lambda: NOW, access_mode="TRADE")
        value = request().model_copy(
            update={
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
                "automation_id": "00000000-0000-4000-8000-000000000002",
                "broker_id": "00000000-0000-4000-8000-000000000003",
                "configuration_fingerprint": "a" * 64,
                "market_valid_until": NOW + timedelta(seconds=5),
            }
        )
        with pytest.raises(TimeoutError):
            await service.dispatch(value, asyncio.get_running_loop().create_future())
        return broker

    broker = asyncio.run(scenario())
    assert broker.reconciliation_calls == [("account-1", "00000000-0000-4000-8000-000000000001")]
    assert broker.position_calls == []
    assert broker.operation_calls == []
    assert broker.dispatch_request.submit_deadline == NOW + timedelta(seconds=1)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_details"),
    [
        pytest.param(
            BrokerOperationError("BROKER_UNAVAILABLE", "Safe broker message", retryable=True),
            "BROKER_UNAVAILABLE",
            "Safe broker message",
            id="typed-broker-error",
        ),
        pytest.param(
            RuntimeError("SECRET_UNEXPECTED_DISPATCH_DETAIL"),
            None,
            "Unexpected dispatch failure",
            id="unexpected-error",
        ),
    ],
)
def test_ambiguous_dispatch_audit_preserves_only_safe_error_details(failure, expected_code, expected_details):
    async def scenario():
        release = asyncio.Event()
        release.set()
        broker = Broker(release, error=failure)
        records = []

        class OrderedRepository(Repository):
            def update_intent(self, idempotency_key, **values):
                if values["state"] == "UNCERTAIN":
                    assert broker.reconciliation_calls == [("account-1", "intent-1")]
                    assert broker.position_calls == [("account-1", "instrument-1")]
                    assert broker.operation_calls == [("account-1", "instrument-1", 20)]
                super().update_intent(idempotency_key, **values)

        class Audit:
            def record_order_stage(self, **values):
                records.append(values)

        repository = OrderedRepository()
        service = OrderDispatchService(repository, broker, now=lambda: NOW, audit=Audit(), access_mode="TRADE")
        value = request().model_copy(update={"process_id": "process"})
        started = asyncio.get_running_loop().create_future()

        with pytest.raises(type(failure)) as caught:
            await service.dispatch(value, started)

        assert caught.value is failure
        assert started.result() == NOW
        assert [update[1]["state"] for update in repository.updates] == ["SUBMITTING", "UNCERTAIN"]
        assert [record["stage"] for record in records] == [
            BusinessAuditStage.BROKER_ORDER_DISPATCH_STARTED,
            BusinessAuditStage.TRADING_STEP_FAILED,
        ]
        failed = records[-1]
        assert failed["broker_order_state"] == "UNCERTAIN"
        assert failed["exception_type"] == type(failure).__name__
        assert failed["broker_error_code"] == expected_code
        assert failed["broker_error_details"] == expected_details
        assert failed["position_snapshot"] == BrokerPosition(
            "instrument-1", Decimal(2), Decimal(100), Decimal(101), "RUB"
        ).model_dump(mode="json")
        assert failed["recent_operations"] == [
            BrokerRecoveryOperation(
                "operation", OrderSide.BUY, True, NOW, Decimal(1), Decimal(100), Decimal(1), "RUB"
            ).model_dump(mode="json")
        ]
        assert "SECRET_UNEXPECTED_DISPATCH_DETAIL" not in repr(records)

    asyncio.run(scenario())
