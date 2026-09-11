import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.orm import sessionmaker

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerOrderState
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.services.uncertain_intent_reconciliation import (
    UncertainIntentReconciliationService,
)
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 7, 13, 21, tzinfo=UTC)
CREATED_AT = NOW - timedelta(seconds=11)


def command() -> AutomationCommand:
    return baseline_command(lot_size=1)


def intent():
    return SimpleNamespace(
        idempotency_key="intent",
        automation_id=str(command().automation_id),
        kind="SELL_PART",
        side="SELL",
        state="UNCERTAIN",
        quantity_lots=1,
        limit_price=Decimal("253.08"),
        estimated_commission=Decimal("0.12654"),
        broker_order_id="broker-order",
        created_at=CREATED_AT,
    )


class Repository:
    def __init__(self, *, ledger_lots: int) -> None:
        self.ledger_lots = ledger_lots
        self.updates = []
        self.finalizations = []
        self.holds = []
        self.active_intent = intent()

    def get_active_intent(self, automation_id):
        return self.active_intent

    def list_open_lots(self, automation_id):
        return [SimpleNamespace(remaining_lots=self.ledger_lots)]

    def update_intent(self, intent_id, **values):
        self.updates.append((intent_id, values))

    def finalize_execution(self, finalization, *, ledger_already_applied=False):
        self.finalizations.append((finalization, ledger_already_applied))

    def hold_active(self, reason, automation_id=None):
        self.holds.append((reason, automation_id))


class Broker:
    def __init__(self, operations, *, position_lots: str | None = "5", exact_state=None) -> None:
        self.operations = operations
        self.position_lots = position_lots
        self.exact_state = exact_state

    async def get_order_state(self, account_id, broker_order_id):
        return self.exact_state

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        return None

    async def inspect_position(self, account_id, instrument_id):
        if self.position_lots is None:
            return None
        return {"quantity_lots": self.position_lots, "currency": "RUB"}

    async def inspect_recent_operations(self, account_id, instrument_id, limit):
        return self.operations


class Cash:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, intent_id: str, state: str) -> None:
        self.calls.append((intent_id, state))


def sell_operation(*, operation_id: str = "operation") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "operation_type": "OPERATION_TYPE_SELL",
        "state": "OPERATION_STATE_EXECUTED",
        "occurred_at": (CREATED_AT + timedelta(milliseconds=151)).isoformat(),
        "quantity_done": "1",
        "price": "253.08",
        "commission": "-0.12654",
        "currency": "RUB",
    }


def filled_order_state() -> BrokerOrderState:
    return BrokerOrderState(
        "broker-order",
        "intent",
        "FILLED",
        1,
        1,
        Decimal("253.08"),
        Decimal("253.08"),
        Decimal("0.12654"),
        Decimal("0.12654"),
        "RUB",
        executed_price=Decimal("253.08"),
        executed_at=CREATED_AT + timedelta(milliseconds=151),
    )


def test_exact_operation_resolves_uncertain_without_double_applying_ledger() -> None:
    async def scenario():
        repository = Repository(ledger_lots=5)
        cash = Cash()
        service = UncertainIntentReconciliationService(
            repository,
            Broker((sell_operation(),)),
            now=lambda: NOW,
            cash=cash,
        )
        result = await service.reconcile((command(),))
        return repository, cash, result

    repository, cash, result = asyncio.run(scenario())

    assert result.resolved == 1
    assert repository.updates == []
    assert repository.finalizations[0][0].state == "FILLED"
    assert repository.finalizations[0][0].executed_lots == 1
    assert repository.finalizations[0][0].executed_price == Decimal("253.08")
    assert repository.finalizations[0][1] is True
    assert cash.calls == [("intent", "FILLED")]
    assert repository.holds == []


def test_exact_operation_applies_missing_sell_to_pre_execution_ledger() -> None:
    async def scenario():
        repository = Repository(ledger_lots=6)
        service = UncertainIntentReconciliationService(
            repository,
            Broker((sell_operation(),)),
            now=lambda: NOW,
        )
        await service.reconcile((command(),))
        return repository

    repository = asyncio.run(scenario())

    assert repository.finalizations[0][1] is False


def test_final_sell_reconciles_when_broker_no_longer_returns_position() -> None:
    async def scenario():
        repository = Repository(ledger_lots=1)
        cash = Cash()
        service = UncertainIntentReconciliationService(
            repository,
            Broker((sell_operation(),), position_lots=None),
            now=lambda: NOW,
            cash=cash,
        )
        result = await service.reconcile((command(),))
        return repository, cash, result

    repository, cash, result = asyncio.run(scenario())

    assert result.resolved == 1
    assert repository.finalizations[0][0].state == "FILLED"
    assert repository.finalizations[0][1] is False
    assert cash.calls == [("intent", "FILLED")]


def test_ambiguous_operations_keep_uncertain_and_hold_automation() -> None:
    async def scenario():
        repository = Repository(ledger_lots=5)
        cash = Cash()
        service = UncertainIntentReconciliationService(
            repository,
            Broker((sell_operation(operation_id="one"), sell_operation(operation_id="two"))),
            now=lambda: NOW,
            cash=cash,
        )
        result = await service.reconcile((command(),))
        return repository, cash, result

    repository, cash, result = asyncio.run(scenario())

    assert result.unresolved == 1
    assert repository.updates == []
    assert repository.holds == [
        (
            "Uncertain broker intent could not be reconciled unambiguously",
            str(command().automation_id),
        )
    ]
    assert cash.calls == []


def test_exact_broker_order_state_wins_over_ambiguous_operation_search() -> None:
    async def scenario():
        repository = Repository(ledger_lots=5)
        service = UncertainIntentReconciliationService(
            repository,
            Broker(
                (sell_operation(operation_id="one"), sell_operation(operation_id="two")),
                exact_state=filled_order_state(),
            ),
            now=lambda: NOW,
        )
        result = await service.reconcile((command(),))
        return repository, result

    repository, result = asyncio.run(scenario())

    assert result.resolved == 1
    assert repository.holds == []
    assert repository.finalizations[0][0].state == "FILLED"


def test_resolved_uncertain_intent_clears_matching_active_gate() -> None:
    async def scenario():
        gate = ActiveIntentGateService()
        automation_id = str(command().automation_id)
        gate.activate(automation_id, "intent")
        service = UncertainIntentReconciliationService(
            Repository(ledger_lots=5),
            Broker((sell_operation(),)),
            now=lambda: NOW,
            active_intents=gate,
        )

        await service.reconcile((command(),))
        return gate

    gate = asyncio.run(scenario())

    assert gate.has_active_intent(str(command().automation_id)) is False


def test_retryable_broker_failure_allows_later_reconciliation_attempt() -> None:
    class RateLimitedOnceBroker(Broker):
        def __init__(self) -> None:
            super().__init__((sell_operation(),))
            self.calls = 0

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            self.calls += 1
            if self.calls == 1:
                raise TInvestAdapterError("BROKER_RATE_LIMITED", "Rate limited.", retryable=True)
            return self.operations

    async def scenario():
        repository = Repository(ledger_lots=5)
        broker = RateLimitedOnceBroker()
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
        )
        with pytest.raises(TInvestAdapterError):
            await service.reconcile((command(),))
        result = await service.reconcile((command(),))
        return broker.calls, repository, result

    calls, repository, result = asyncio.run(scenario())

    assert calls == 2
    assert result.resolved == 1
    assert repository.finalizations[0][0].state == "FILLED"


def test_operation_reconciliation_normalizes_broker_timestamp_to_milliseconds() -> None:
    async def scenario():
        repository = Repository(ledger_lots=5)
        operation = sell_operation()
        operation["occurred_at"] = (CREATED_AT + timedelta(milliseconds=151, microseconds=456)).isoformat()
        service = UncertainIntentReconciliationService(
            repository,
            Broker((operation,)),
            now=lambda: NOW,
        )
        result = await service.reconcile((command(),))
        return repository, result

    repository, result = asyncio.run(scenario())

    assert result.resolved == 1
    assert repository.finalizations[0][0].executed_at.microsecond % 1000 == 0


@pytest.fixture
def durable_audit(tmp_path):
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'audit.sqlite'}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(
        sessionmaker(engine, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
    )
    repository.cache_command(command())
    try:
        yield BusinessAuditService(repository, now=lambda: NOW), repository
    finally:
        engine.dispose()


def audit_payloads(repository):
    return [fact.payload for fact in repository.ready_fact_outbox(100, now=NOW, deadline_ms=0)]


def test_recovery_audit_and_finalization_share_process_after_cleanup(durable_audit):
    audit, outbox = durable_audit
    repository = Repository(ledger_lots=5)
    gate = ActiveIntentGateService()
    gate.activate(str(command().automation_id), "intent")

    class InspectingCash(Cash):
        async def complete(self, intent_id, state):
            assert repository.finalizations
            assert BusinessAuditStage.WORKER_RECOVERY_COMPLETED.value not in {
                item["stage"] for item in audit_payloads(outbox)
            }
            await super().complete(intent_id, state)

    cash = InspectingCash()
    service = UncertainIntentReconciliationService(
        repository, Broker((sell_operation(),)), now=lambda: NOW, audit=audit, cash=cash, active_intents=gate
    )
    result = asyncio.run(service.reconcile((command(),)))

    payloads = audit_payloads(outbox)
    assert [item["stage"] for item in payloads] == [
        BusinessAuditStage.WORKER_RECOVERY_STARTED.value,
        BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE.value,
        BusinessAuditStage.WORKER_RECOVERY_COMPLETED.value,
    ]
    process_id = repository.finalizations[0][0].process_id
    assert UUID(process_id).version == 5
    assert {item["process_id"] for item in payloads} == {process_id}
    assert all(datetime.fromisoformat(item["occurred_at"]) == NOW for item in payloads)
    assert result.resolved == 1
    assert not gate.has_active_intent(str(command().automation_id))
    assert cash.calls == [("intent", "FILLED")]


def test_ambiguous_recovery_keeps_start_and_hold_in_one_process_without_completion(durable_audit):
    audit, outbox = durable_audit
    service = UncertainIntentReconciliationService(Repository(ledger_lots=5), Broker(()), now=lambda: NOW, audit=audit)
    result = asyncio.run(service.reconcile((command(),)))

    payloads = audit_payloads(outbox)
    assert result.unresolved == 1
    assert [item["stage"] for item in payloads] == [
        BusinessAuditStage.WORKER_RECOVERY_STARTED.value,
        BusinessAuditStage.AUTOMATION_MOVED_TO_HOLD.value,
    ]
    assert len({item["process_id"] for item in payloads}) == 1


def test_recovery_process_survives_retry_and_service_restart(durable_audit):
    audit, outbox = durable_audit
    repository = Repository(ledger_lots=5)

    class UnavailableBroker(Broker):
        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            raise TInvestAdapterError("BROKER_RATE_LIMITED", "Rate limited.", retryable=True)

    async def scenario():
        service = UncertainIntentReconciliationService(repository, UnavailableBroker(()), now=lambda: NOW, audit=audit)
        for _ in range(2):
            with pytest.raises(TInvestAdapterError):
                await service.reconcile((command(),))
        restarted = UncertainIntentReconciliationService(
            repository, Broker((sell_operation(),)), now=lambda: NOW, audit=audit
        )
        return await restarted.reconcile((command(),))

    result = asyncio.run(scenario())
    payloads = audit_payloads(outbox)
    assert result.resolved == 1
    assert [item["stage"] for item in payloads].count(BusinessAuditStage.WORKER_RECOVERY_STARTED.value) == 3
    assert {item["process_id"] for item in payloads} == {repository.finalizations[0][0].process_id}
    assert len({item["audit_event_id"] for item in payloads}) == len(payloads)


def test_failed_recovery_start_audit_allows_retry_with_same_process(durable_audit):
    _, outbox = durable_audit
    repository = Repository(ledger_lots=5)

    class FailingOnceAuditRepository:
        failed_process_id = None

        def append_audit_events(self, events):
            if self.failed_process_id is None:
                self.failed_process_id = events[0].process_id
                raise RuntimeError("Synthetic temporary audit failure")
            outbox.append_audit_events(events)

    class CountingBroker(Broker):
        operation_reads = 0

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            self.operation_reads += 1
            return await super().inspect_recent_operations(account_id, instrument_id, limit)

    failing_audit = FailingOnceAuditRepository()
    broker = CountingBroker((sell_operation(),))
    service = UncertainIntentReconciliationService(
        repository,
        broker,
        now=lambda: NOW,
        audit=BusinessAuditService(failing_audit, now=lambda: NOW),
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="Synthetic temporary audit failure"):
            await service.reconcile((command(),))
        assert broker.operation_reads == 0
        assert repository.finalizations == []
        assert audit_payloads(outbox) == []
        return await service.reconcile((command(),))

    result = asyncio.run(scenario())
    assert result.resolved == 1
    assert broker.operation_reads == 1
    assert repository.finalizations[0][0].process_id == failing_audit.failed_process_id
    payloads = audit_payloads(outbox)
    assert {item["process_id"] for item in payloads} == {failing_audit.failed_process_id}
    assert [item["stage"] for item in payloads] == [
        BusinessAuditStage.WORKER_RECOVERY_STARTED.value,
        BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE.value,
        BusinessAuditStage.WORKER_RECOVERY_COMPLETED.value,
    ]


@pytest.mark.parametrize("scope", ["intent", "automation", "broker"])
def test_recovery_process_distinguishes_intent_and_scope(durable_audit, scope):
    audit, outbox = durable_audit

    async def scenario():
        ids = []
        for changed in (False, True):
            command_value = baseline_command(
                lot_size=1, **({scope: "different"} if changed and scope != "intent" else {})
            )
            outbox.cache_command(command_value)
            repository = Repository(ledger_lots=5)
            if changed and scope == "intent":
                repository.active_intent.idempotency_key = "different-intent"
            service = UncertainIntentReconciliationService(
                repository, Broker((sell_operation(),)), now=lambda: NOW, audit=audit
            )
            await service.reconcile((command_value,))
            ids.append(repository.finalizations[0][0].process_id)
        return ids

    first, second = asyncio.run(scenario())
    assert first != second
    assert UUID(first).version == UUID(second).version == 5


@pytest.mark.parametrize("failure", ["persist", "cash", "gate"])
def test_failed_recovery_never_reports_completion(durable_audit, failure):
    audit, outbox = durable_audit

    class FailingRepository(Repository):
        def finalize_execution(self, finalization, *, ledger_already_applied=False):
            if failure == "persist":
                raise RuntimeError("Synthetic persistence failure")
            return super().finalize_execution(finalization, ledger_already_applied=ledger_already_applied)

    class FailingCash(Cash):
        async def complete(self, intent_id, state):
            if failure == "cash":
                raise RuntimeError("Synthetic cash failure")
            await super().complete(intent_id, state)

    class FailingGate(ActiveIntentGateService):
        def clear(self, automation_id, intent_id):
            if failure == "gate":
                raise RuntimeError("Synthetic gate failure")
            super().clear(automation_id, intent_id)

    service = UncertainIntentReconciliationService(
        FailingRepository(ledger_lots=5),
        Broker((sell_operation(),)),
        now=lambda: NOW,
        audit=audit,
        cash=FailingCash(),
        active_intents=FailingGate(),
    )
    with pytest.raises(RuntimeError, match="Synthetic"):
        asyncio.run(service.reconcile((command(),)))

    stages = [item["stage"] for item in audit_payloads(outbox)]
    assert BusinessAuditStage.WORKER_RECOVERY_STARTED.value in stages
    assert BusinessAuditStage.WORKER_RECOVERY_COMPLETED.value not in stages
