import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition, BrokerRecoveryOperation, OrderSide
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_admission import (
    PreRpcAbortProof,
    SubmitAbsenceBasis,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    submit_request_fingerprint,
)
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import filled_buy
from trading_automaton.adapters.tinvest_broker_session import _order_state
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.services.account_cash_reservation import AccountCashReservationService
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.services.order_lifecycle import intent_deadline
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.services.uncertain_intent_reconciliation import (
    UncertainIntentReconciliationService,
)
from trading_automaton.storage.database import create_worker_engine, initialize_worker_schema
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, LocalIntentModel
from trading_automaton.storage.repository import ExecutionFinalization, LocalAutomationRepository

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
        limit_price=Decimal("100"),
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
        self.state_calls = []
        self.cancel_calls = []
        self.followup_states = []

    async def get_order_state(self, account_id, broker_order_id):
        self.state_calls.append((account_id, broker_order_id))
        return self.followup_states.pop(0) if self.followup_states else self.exact_state

    async def cancel_order(self, account_id, broker_order_id):
        self.cancel_calls.append((account_id, broker_order_id))

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        return self.exact_state

    async def inspect_position(self, account_id, instrument_id):
        if self.position_lots is None:
            return None
        return BrokerPosition(instrument_id, Decimal(self.position_lots), Decimal(), Decimal(), "RUB")

    async def inspect_recent_operations(self, account_id, instrument_id, limit):
        return self.operations


class Cash:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, intent_id: str, state: str) -> None:
        self.calls.append((intent_id, state))


def sell_operation(*, operation_id: str = "operation") -> BrokerRecoveryOperation:
    return BrokerRecoveryOperation(
        operation_id,
        OrderSide.SELL,
        True,
        CREATED_AT + timedelta(milliseconds=151),
        Decimal(1),
        Decimal("253.08"),
        Decimal("-0.12654"),
        "RUB",
    )


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


def test_expired_known_nonterminal_order_is_cancelled_and_status_confirmed_without_portfolio():
    class PortfolioUnavailableBroker(Broker):
        async def inspect_position(self, account_id, instrument_id):
            raise AssertionError("Zero-fill cancellation must not require the portfolio endpoint")

    class Admission:
        def __init__(self):
            self.cancel_checks = 0

        async def submit_attempt(self, *args, **kwargs):
            return SimpleNamespace(proven_not_granted=False)

        async def validate_cancel(self):
            self.cancel_checks += 1
            return True

    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = SimpleNamespace(
            **{
                **vars(intent()),
                "state": "ACCEPTED",
                "dispatch_started_at": NOW - timedelta(seconds=10),
                "strategy_snapshot": {
                    "order_ttl_seconds": 10,
                    "admission_origin": {
                        "runtime_instance_id": "00000000-0000-4000-8000-000000000099",
                        "generation": 1,
                        "revision": 1,
                        "configuration_fingerprint": "a" * 64,
                    },
                },
            }
        )
        assert intent_deadline(repository.active_intent, now=NOW) == NOW
        accepted = BrokerOrderState(
            "broker-order",
            "intent",
            "ACCEPTED",
            1,
            0,
            Decimal("100"),
            Decimal(),
            Decimal("0.1"),
            Decimal(),
            "RUB",
        )
        cancelled = accepted.model_copy(update={"status": "CANCELLED"})
        broker = PortfolioUnavailableBroker((), position_lots=None, exact_state=accepted)
        broker.followup_states = [cancelled]
        admission = Admission()
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            access_mode="TRADE",
            admission=admission,
        )
        result = await service.reconcile((command(),))
        return repository, broker, result, admission

    repository, broker, result, admission = asyncio.run(scenario())

    assert broker.state_calls == [(command().account_id, "broker-order")]
    assert admission.cancel_checks == 1
    assert broker.cancel_calls == [(command().account_id, "broker-order")]
    assert result.resolved == 1
    assert repository.finalizations[0][0].state == "CANCELLED"


def test_fresh_nonterminal_prod_order_remains_active_until_its_persisted_deadline():
    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = SimpleNamespace(
            **{
                **vars(intent()),
                "state": "ACCEPTED",
                "dispatch_started_at": NOW - timedelta(seconds=9),
                "strategy_snapshot": {"order_ttl_seconds": 10},
            }
        )
        state = BrokerOrderState(
            "broker-order",
            "intent",
            "ACCEPTED",
            1,
            0,
            Decimal("100"),
            Decimal(),
            Decimal("0.1"),
            Decimal(),
            "RUB",
        )
        broker = Broker((), exact_state=state)
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            access_mode="TRADE",
        )
        result = await service.reconcile((command(),))
        return repository, broker, result

    repository, broker, result = asyncio.run(scenario())

    assert result.resolved == result.unresolved == 0
    assert broker.cancel_calls == []
    assert repository.holds == []
    assert repository.updates == []


def test_restart_preserves_deadline_from_dispatch_timestamp_and_intent_snapshot(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'durable-order-deadline.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = LocalAutomationRepository(factory, environment="PROD")
    value = command()
    repository.cache_command(value)
    item = decision_item(value).model_copy(
        update={"strategy_snapshot": {**decision_item(value).strategy_snapshot, "order_ttl_seconds": 10}}
    )
    intent_id = repository.save_decision_batch((item,), occurred_at=NOW).intents[0].idempotency_key
    started_at = NOW + timedelta(seconds=1)
    repository.update_intent(
        intent_id,
        state="SUBMITTING",
        occurred_at=started_at,
        dispatch_started_at=started_at,
    )
    ttl = repository.get_active_intent(str(value.automation_id)).strategy_snapshot["order_ttl_seconds"]
    engine.dispose()

    reopened_engine = create_worker_engine(database_url)
    reopened = LocalAutomationRepository(sessionmaker(reopened_engine, expire_on_commit=False), environment="PROD")
    restored = reopened.get_active_intent(str(value.automation_id))

    assert restored.dispatch_started_at == started_at
    assert intent_deadline(restored, now=started_at + timedelta(seconds=ttl + 30)) == started_at + timedelta(
        seconds=ttl
    )
    reopened_engine.dispose()


def test_cancel_confirmation_finalizes_actual_partial_fill_and_commission():
    class Admission:
        async def validate_cancel(self):
            return True

    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = SimpleNamespace(
            **{
                **vars(intent()),
                "state": "PARTIALLY_FILLED",
                "dispatch_started_at": NOW - timedelta(seconds=10),
                "strategy_snapshot": {
                    "order_ttl_seconds": 10,
                    "admission_origin": {
                        "runtime_instance_id": "00000000-0000-4000-8000-000000000099",
                        "generation": 1,
                        "revision": 1,
                        "configuration_fingerprint": "a" * 64,
                    },
                },
            }
        )
        accepted = BrokerOrderState(
            "broker-order",
            "intent",
            "PARTIALLY_FILLED",
            2,
            0,
            Decimal("200"),
            Decimal(),
            Decimal("0.2"),
            Decimal(),
            "RUB",
        )
        cancelled = accepted.model_copy(
            update={
                "status": "CANCELLED",
                "executed_lots": 1,
                "executed_amount": Decimal("101"),
                "executed_commission": Decimal("0.25"),
                "executed_price": Decimal("101"),
                "executed_at": NOW,
            }
        )
        broker = Broker((), position_lots="4", exact_state=accepted)
        broker.followup_states = [cancelled]
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            access_mode="TRADE",
            admission=Admission(),
        )
        result = await service.reconcile((command(),))
        return repository, broker, result

    repository, broker, result = asyncio.run(scenario())

    finalization = repository.finalizations[0][0]
    assert result.resolved == 1
    assert broker.cancel_calls == [(command().account_id, "broker-order")]
    assert finalization.state == "CANCELLED"
    assert finalization.executed_lots == 1
    assert finalization.executed_price == Decimal("101")
    assert finalization.executed_commission == Decimal("0.25")


def test_recovery_skips_intent_owned_by_live_tracking_watcher():
    async def scenario():
        repository = Repository(ledger_lots=5)
        broker = Broker((), exact_state=filled_order_state())
        gate = ActiveIntentGateService()
        assert gate.claim_tracking("intent")
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            active_intents=gate,
        )

        result = await service.reconcile((command(),))
        return broker, result

    broker, result = asyncio.run(scenario())

    assert result.resolved == 0
    assert broker.state_calls == []


def test_legacy_nonterminal_order_without_snapshot_ttl_is_held_without_env_fallback():
    class Admission:
        async def submit_attempt(self, *args, **kwargs):
            return SimpleNamespace(proven_not_granted=False)

        async def validate_cancel(self):
            return True

    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = SimpleNamespace(
            **{
                **vars(intent()),
                "state": "ACCEPTED",
                "dispatch_started_at": NOW - timedelta(days=1),
                "strategy_snapshot": {
                    "admission_origin": {
                        "runtime_instance_id": "00000000-0000-4000-8000-000000000099",
                        "generation": 1,
                        "revision": 1,
                        "configuration_fingerprint": "a" * 64,
                    },
                },
            }
        )
        accepted = BrokerOrderState(
            "broker-order",
            "intent",
            "ACCEPTED",
            1,
            0,
            Decimal("100"),
            Decimal(),
            Decimal("0.1"),
            Decimal(),
            "RUB",
        )
        broker = Broker((), exact_state=accepted)
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            access_mode="TRADE",
            admission=Admission(),
        )
        result = await service.reconcile((command(),))
        return repository, broker, result

    repository, broker, result = asyncio.run(scenario())

    assert result.unresolved == 1
    assert broker.cancel_calls == []
    assert repository.holds
    assert repository.active_intent.state == "ACCEPTED"
    assert repository.updates[-1][0] == "intent"
    assert repository.updates[-1][1]["state"] == "UNCERTAIN"


def test_unknown_sdk_status_is_not_cancelled_even_with_matching_order_identity():
    class Admission:
        async def submit_attempt(self, *args, **kwargs):
            return SimpleNamespace(proven_not_granted=False)

        async def validate_cancel(self):
            return True

    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = SimpleNamespace(
            **{
                **vars(intent()),
                "state": "ACCEPTED",
                "dispatch_started_at": NOW - timedelta(seconds=20),
                "strategy_snapshot": {
                    "order_ttl_seconds": 10,
                    "admission_origin": {
                        "runtime_instance_id": "00000000-0000-4000-8000-000000000099",
                        "generation": 1,
                        "revision": 1,
                        "configuration_fingerprint": "a" * 64,
                    },
                },
            }
        )
        money = SimpleNamespace(units=100, nano=0, currency="rub")
        unknown = _order_state(
            SimpleNamespace(
                order_id="broker-order",
                order_request_id="intent",
                execution_report_status="EXECUTION_REPORT_STATUS_UNSPECIFIED",
                lots_requested=1,
                lots_executed=0,
                initial_order_price=money,
                total_order_amount=SimpleNamespace(units=0, nano=0, currency="rub"),
                initial_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
                executed_commission=SimpleNamespace(units=0, nano=0, currency="rub"),
                executed_order_price=SimpleNamespace(units=0, nano=0, currency="rub"),
            )
        )
        broker = Broker((), exact_state=unknown)
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            access_mode="TRADE",
            admission=Admission(),
        )
        await service.reconcile((command(),))
        return repository, broker

    repository, broker = asyncio.run(scenario())

    assert broker.cancel_calls == []
    assert repository.updates[-1][1]["state"] == "UNCERTAIN"


def test_unknown_cancel_outcome_persists_uncertain_then_finalizes_without_resume(tmp_path):
    class Admission:
        async def validate_cancel(self):
            return True

        async def submit_attempt(self, *args, **kwargs):
            return SimpleNamespace(proven_not_granted=False)

    value = command()
    automation_id = str(value.automation_id)
    database_url = f"sqlite:///{tmp_path / 'uncertain-cancel.db'}"
    engine = create_worker_engine(database_url)
    initialize_worker_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    clock = [NOW]
    repository = LocalAutomationRepository(
        factory, fact_writer=FactOutboxWriter(clock=lambda: clock[0]), environment="PROD"
    )
    repository.cache_command(value)
    item = decision_item(value).model_copy(
        update={
            "strategy_snapshot": {
                "order_ttl_seconds": 10,
                "admission_origin": {
                    "runtime_instance_id": "00000000-0000-4000-8000-000000000099",
                    "generation": 1,
                    "revision": 1,
                    "configuration_fingerprint": "a" * 64,
                },
            }
        }
    )
    key = repository.save_decision_batch((item,), occurred_at=NOW).intents[0].idempotency_key
    started_at = NOW - timedelta(seconds=10)
    repository.update_intent(
        key,
        state="ACCEPTED",
        occurred_at=NOW - timedelta(seconds=1),
        dispatch_started_at=started_at,
        broker_order_id="broker-order",
    )
    accepted = BrokerOrderState(
        "broker-order", key, "ACCEPTED", 1, 0, Decimal("201"), Decimal(), Decimal("1"), Decimal(), "RUB"
    )
    broker = Broker((), position_lots=None, exact_state=accepted)
    broker.followup_states = [accepted, accepted]

    async def scenario():
        cash = AccountCashReservationService()
        await cash.restore(tuple(repository.list_active_buy_intent_reservations()))
        reserved_before = await cash.reserved(value.account_id, "RUB")
        recovery = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: clock[0],
            environment="PROD",
            access_mode="TRADE",
            admission=Admission(),
            cash=cash,
            sleep=lambda _delay: asyncio.sleep(0),
            poll_limit=2,
        )
        first = await recovery.reconcile((value,))
        retained = repository.get_active_intent(automation_id)
        assert retained is not None
        assert retained.state == "UNCERTAIN"
        assert await cash.reserved(value.account_id, "RUB") == reserved_before
        assert repository.get_state(automation_id).state == "HOLD"
        clock[0] += timedelta(seconds=31)
        broker.exact_state = accepted.model_copy(update={"status": "CANCELLED"})
        second = await recovery.reconcile((value,))
        assert second.resolved == 1
        assert repository.get_active_intent(automation_id) is None
        assert repository.get_state(automation_id).state == "HOLD"
        assert await cash.reserved(value.account_id, "RUB") == 0
        facts = repository.ready_fact_outbox(100, now=clock[0] + timedelta(seconds=1), deadline_ms=0)
        repeated = await recovery.reconcile((value,))
        assert repeated.resolved == repeated.unresolved == 0
        assert repository.ready_fact_outbox(100, now=clock[0] + timedelta(seconds=1), deadline_ms=0) == facts
        return first, broker, repository, facts

    first, broker, repository, facts = asyncio.run(scenario())
    assert first.unresolved == 1
    assert broker.cancel_calls == [(value.account_id, "broker-order")]
    assert repository.get_latest_intent(automation_id).state == "CANCELLED"
    assert not any(fact.fact_kind == "TRADE_EXECUTION_RECORDED" for fact in facts)
    engine.dispose()


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


def test_prod_recovery_never_fabricates_fill_from_matching_operations():
    class TrackingBroker(Broker):
        operation_reads = 0

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            self.operation_reads += 1
            return await super().inspect_recent_operations(account_id, instrument_id, limit)

    async def scenario():
        repository = Repository(ledger_lots=5)
        broker = TrackingBroker((sell_operation(),))
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
        )
        result = await service.reconcile((command(),))
        return repository, broker, result

    repository, broker, result = asyncio.run(scenario())
    assert result.unresolved == 1
    assert broker.operation_reads == 0
    assert repository.finalizations == []
    assert repository.holds


def test_retired_generation_proven_no_grant_cancels_queued_intent_without_broker_lookup():
    class Admission:
        async def submit_attempt(self, idempotency_key, *, origin_runtime_instance_id, origin_generation):
            assert idempotency_key == intent_value.idempotency_key
            assert origin_runtime_instance_id == UUID("00000000-0000-4000-8000-000000000801")
            assert origin_generation == 3
            return SubmitAttemptView(
                broker_id=command().broker_id,
                account_id=command().account_id,
                idempotency_key=UUID(idempotency_key),
                consumed_state=None,
                proven_not_granted=True,
                original_runtime_instance_id=origin_runtime_instance_id,
                original_generation=origin_generation,
                original_generation_retired=True,
                absence_basis=SubmitAbsenceBasis.RETIRED_GENERATION,
            )

    class TrackingBroker(Broker):
        lookups = 0

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.lookups += 1
            return await super().find_by_idempotency_key(account_id, idempotency_key)

        async def inspect_position(self, account_id, instrument_id):
            self.lookups += 1
            return await super().inspect_position(account_id, instrument_id)

    intent_value = intent()
    intent_value.state = "SUBMITTING"
    intent_value.idempotency_key = "00000000-0000-4000-8000-000000000802"
    intent_value.broker_order_id = None
    intent_value.requested_amount = Decimal("253.08")
    intent_value.executed_lots = 0
    intent_value.strategy_snapshot = {
        "admission_origin": {
            "runtime_instance_id": "00000000-0000-4000-8000-000000000801",
            "generation": 3,
        }
    }

    async def scenario():
        repository = Repository(ledger_lots=5)
        repository.active_intent = intent_value
        broker = TrackingBroker((sell_operation(),))
        cash = Cash()
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
            environment="PROD",
            admission=Admission(),
            cash=cash,
        )
        result = await service.reconcile((command(),))
        return repository, broker, cash, result

    repository, broker, cash, result = asyncio.run(scenario())
    assert result.resolved == 1
    assert repository.updates[0][1]["state"] == "CANCELLED"
    assert repository.updates[0][1]["terminal_at"] == NOW
    assert broker.lookups == 0
    assert repository.finalizations == []
    assert cash.calls == [(intent_value.idempotency_key, "CANCELLED")]


@pytest.mark.parametrize(
    "state",
    [
        "FAILED",
        "CREATED",
        "DISPATCH_PENDING",
        "SUBMITTING",
        "SUBMITTED",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "UNCERTAIN",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
    ],
)
def test_prod_restart_reconciles_retained_and_unfinalized_terminal_phases_by_request_key(tmp_path, state):
    class RequestBroker(Broker):
        request_lookups = 0
        operation_reads = 0

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.request_lookups += 1
            return rejected_order_state().model_copy(update={"idempotency_key": idempotency_key})

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            self.operation_reads += 1
            return await super().inspect_recent_operations(account_id, instrument_id, limit)

    database_url = f"sqlite:///{tmp_path / 'retained-intent.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW), environment="PROD")
    command_value = command()
    repo.cache_command(command_value)
    batch = repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    intent_id = batch.intents[0].idempotency_key
    with factory.begin() as session:
        session.get(LocalIntentModel, intent_id).state = state
    engine.dispose()

    reopened_engine = create_worker_engine(database_url)
    reopened_factory = sessionmaker(reopened_engine, expire_on_commit=False)
    reopened = LocalAutomationRepository(
        reopened_factory,
        fact_writer=FactOutboxWriter(clock=lambda: NOW + timedelta(seconds=2)),
        environment="PROD",
    )
    broker = RequestBroker(())
    service = UncertainIntentReconciliationService(
        reopened,
        broker,
        now=lambda: NOW + timedelta(seconds=2),
        environment="PROD",
    )

    result = asyncio.run(service.reconcile((command_value,)))

    assert result.resolved == 1
    assert broker.request_lookups == 1
    assert broker.operation_reads == 0
    assert reopened.get_active_intent(str(command_value.automation_id)) is None
    assert reopened.get_latest_intent(str(command_value.automation_id)).state == "REJECTED"
    reopened_engine.dispose()


def test_prod_restart_finalizes_persisted_pre_rpc_proof_without_sdk_lookup(tmp_path):
    class NoBrokerAccess(Broker):
        lookups = 0

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.lookups += 1
            raise AssertionError("Persisted pre-RPC proof must not query the broker")

        async def inspect_position(self, account_id, instrument_id):
            self.lookups += 1
            raise AssertionError("Persisted pre-RPC proof must not inspect positions")

    database_url = f"sqlite:///{tmp_path / 'proof-recovery.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW), environment="PROD")
    command_value = command()
    origin = {
        "runtime_instance_id": "00000000-0000-4000-8000-000000000801",
        "generation": 3,
        "revision": 8,
        "configuration_fingerprint": "d" * 64,
        "lot_size": command_value.lot_size,
        "currency": command_value.currency,
        "instrument_uid": command_value.external_instrument_id,
        "fact_instrument_id": str(command_value.instrument_id),
    }
    decision = decision_item(command_value).model_copy(update={"strategy_snapshot": {"admission_origin": origin}})
    repo.cache_command(command_value)
    batch = repo.save_decision_batch((decision,), occurred_at=NOW)
    intent_id = batch.intents[0].idempotency_key
    repo.update_intent(intent_id, state="SUBMITTING", occurred_at=NOW + timedelta(milliseconds=1))
    authorization = SubmitAuthorizationRequest(
        broker_id=command_value.broker_id,
        account_id=command_value.account_id,
        automation_id=command_value.automation_id,
        idempotency_key=UUID(intent_id),
        generation=origin["generation"],
        revision=origin["revision"],
        configuration_fingerprint=origin["configuration_fingerprint"],
        instrument_uid=command_value.external_instrument_id,
        lot_size=origin["lot_size"],
        currency=origin["currency"],
        side="BUY",
        quantity_lots=1,
        limit_price=Decimal("100"),
        market_valid_until=NOW,
        submit_deadline=NOW,
    )
    proof = PreRpcAbortProof(
        permit_id=UUID("00000000-0000-4000-8000-000000000802"),
        idempotency_key=UUID(intent_id),
        request_fingerprint=submit_request_fingerprint(authorization),
        runtime_instance_id=UUID(origin["runtime_instance_id"]),
        generation=origin["generation"],
        configuration_fingerprint=origin["configuration_fingerprint"],
        aborted_at=NOW,
        reason="PERMIT_EXPIRED_BEFORE_POST",
    )
    repo.update_intent(
        intent_id,
        state="CANCELLED",
        occurred_at=NOW + timedelta(seconds=1),
        requested_amount=Decimal("100"),
        estimated_commission=Decimal(),
        executed_amount=Decimal(),
        executed_commission=Decimal(),
        executed_lots=0,
        executed_price=Decimal(),
        pre_rpc_abort_proof=proof,
    )
    before_cache_state = repo.get_state(str(command_value.automation_id))
    changed_command = command_value.model_copy(
        update={
            "lot_size": 10,
            "currency": "USD",
            "external_instrument_id": "changed-uid",
            "instrument_id": UUID("00000000-0000-4000-8000-000000000899"),
            "revision": before_cache_state.revision,
            "last_sequence_number": before_cache_state.last_sequence_number,
        }
    )
    assert repo.cache_command(changed_command)
    engine.dispose()

    reopened_engine = create_worker_engine(database_url)
    reopened_factory = sessionmaker(reopened_engine, expire_on_commit=False)
    reopened = LocalAutomationRepository(
        reopened_factory,
        fact_writer=FactOutboxWriter(clock=lambda: NOW + timedelta(seconds=2)),
        environment="PROD",
    )
    broker = NoBrokerAccess(())
    service = UncertainIntentReconciliationService(
        reopened,
        broker,
        now=lambda: NOW + timedelta(seconds=2),
        environment="PROD",
    )

    result = asyncio.run(service.reconcile((changed_command,)))

    assert result.resolved == 1
    assert broker.lookups == 0
    terminal = reopened.get_latest_intent(str(command_value.automation_id))
    assert terminal.state == "CANCELLED"
    assert terminal.terminal_at is not None
    order_facts = list(reopened.ready_fact_outbox(20, now=NOW + timedelta(seconds=3), deadline_ms=0))
    proof_facts = [item for item in order_facts if item.payload.get("pre_rpc_abort_proof") is not None]
    assert proof_facts
    assert len(proof_facts) == 1
    assert proof_facts[-1].payload["pre_rpc_abort_proof"] == proof.model_dump(mode="json")
    assert proof_facts[-1].payload["instrument_id"] == origin["fact_instrument_id"]
    assert order_facts[-1].payload.get("pre_rpc_abort_proof") is None

    original_request = DispatchRequest(
        idempotency_key=intent_id,
        account_id=command_value.account_id,
        instrument_id=origin["instrument_uid"],
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("100"),
        automation_id=str(command_value.automation_id),
        lot_size=origin["lot_size"],
        process_id=decision.process_id,
        broker_id=str(command_value.broker_id),
        reservation_currency=origin["currency"],
    )
    original_response = BrokerOrderState(
        broker_order_id="",
        idempotency_key=intent_id,
        status="CANCELLED",
        requested_lots=1,
        executed_lots=0,
        requested_amount=Decimal("100"),
        executed_amount=Decimal(),
        estimated_commission=Decimal(),
        executed_commission=Decimal(),
        currency=origin["currency"],
        pre_rpc_abort_proof=proof,
    )
    facts_before_tracking = reopened.ready_fact_outbox(20, now=NOW + timedelta(seconds=4), deadline_ms=0)

    async def replay_tracking():
        tracking = OrderTrackingService(reopened, now=lambda: NOW + timedelta(seconds=4))
        dispatch_task = asyncio.create_task(asyncio.sleep(0, result=original_response))
        tracking.track(intent_id, dispatch_task, request=original_request)
        await tracking.wait_all()

    asyncio.run(replay_tracking())
    facts_after_tracking = reopened.ready_fact_outbox(20, now=NOW + timedelta(seconds=5), deadline_ms=0)
    assert facts_after_tracking == facts_before_tracking
    assert reopened.get_state(str(command_value.automation_id)).state != "HOLD"

    finalization = ExecutionFinalization(
        intent_id=intent_id,
        automation_id=str(command_value.automation_id),
        broker_id=str(command_value.broker_id),
        account_id=command_value.account_id,
        instrument_id=origin["instrument_uid"],
        side="BUY",
        quantity_lots=1,
        requested_price=Decimal("100"),
        currency=origin["currency"],
        state="CANCELLED",
        occurred_at=NOW + timedelta(seconds=4),
        broker_order_id="",
        requested_amount=Decimal("100"),
        executed_amount=Decimal(),
        estimated_commission=Decimal(),
        executed_commission=Decimal(),
        executed_lots=0,
        executed_price=Decimal(),
        executed_at=None,
        terminal_at=NOW + timedelta(seconds=4),
        lot_size=origin["lot_size"],
        process_id=decision.process_id,
    )
    for mismatch in (
        {"lot_size": 10},
        {"currency": "USD"},
        {"instrument_id": "changed-uid"},
        {"requested_amount": Decimal("101")},
    ):
        with pytest.raises(ValueError, match="conflicts"):
            reopened.finalize_execution(finalization.model_copy(update=mismatch))
    reopened_engine.dispose()


@pytest.mark.parametrize("terminal_status", ["FILLED", "CANCELLED"])
def test_prod_restart_finalizes_positive_execution_once_with_fees_and_ledger(tmp_path, terminal_status):
    class ExactStateBroker(Broker):
        def __init__(self, state, position_lots):
            super().__init__((), position_lots=str(position_lots), exact_state=state)
            self.request_lookups = 0

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.request_lookups += 1
            assert account_id == command().account_id
            assert idempotency_key == self.exact_state.idempotency_key
            return self.exact_state

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            raise AssertionError("PROD recovery must not infer execution from operations")

    database_url = f"sqlite:///{tmp_path / f'positive-{terminal_status.lower()}.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW), environment="PROD")
    command_value = command()
    repo.cache_command(command_value)

    if terminal_status == "FILLED":
        intent_id = "00000000-0000-4000-8000-000000000901"
        item = decision_item(command_value, intent_id=intent_id)
        expected_position = 1
        exact_state = BrokerOrderState(
            "filled-order",
            intent_id,
            "FILLED",
            1,
            1,
            Decimal("100"),
            Decimal("101"),
            Decimal("0.20"),
            Decimal("0.25"),
            "RUB",
            executed_price=Decimal("101"),
            executed_at=NOW + timedelta(seconds=2),
        )
    else:
        seed_item = decision_item(command_value, intent_id="00000000-0000-4000-8000-000000000902").model_copy(
            update={
                "intent": decision_item(command_value).intent.model_copy(
                    update={
                        "idempotency_key": "00000000-0000-4000-8000-000000000902",
                        "quantity_lots": 2,
                        "limit_price": Decimal("100"),
                    }
                ),
                "quantity_lots": 2,
                "decision_quantity_lots": 2,
            }
        )
        repo.save_decision_batch((seed_item,), occurred_at=NOW)
        repo.finalize_execution(
            filled_buy().model_copy(
                update={
                    "intent_id": "00000000-0000-4000-8000-000000000902",
                    "automation_id": str(command_value.automation_id),
                    "broker_id": str(command_value.broker_id),
                    "account_id": command_value.account_id,
                    "instrument_id": command_value.external_instrument_id,
                    "quantity_lots": 2,
                    "executed_lots": 2,
                    "requested_amount": Decimal("200"),
                    "executed_amount": Decimal("200"),
                    "estimated_commission": Decimal("0.40"),
                    "executed_commission": Decimal("0.50"),
                    "lot_size": 1,
                }
            )
        )
        intent_id = "00000000-0000-4000-8000-000000000903"
        item = decision_item(command_value, intent_id=intent_id).model_copy(
            update={
                "decision": "SELL_PART",
                "intent": decision_item(command_value).intent.model_copy(
                    update={"idempotency_key": intent_id, "kind": "SELL_PART", "side": "SELL", "quantity_lots": 2}
                ),
                "quantity_lots": 2,
                "decision_quantity_lots": 2,
            }
        )
        expected_position = 1
        exact_state = BrokerOrderState(
            "partial-cancel-order",
            intent_id,
            "CANCELLED",
            2,
            1,
            Decimal("200"),
            Decimal("101"),
            Decimal("0.40"),
            Decimal("0.25"),
            "RUB",
            executed_price=Decimal("101"),
            executed_at=NOW + timedelta(seconds=2),
        )

    item_time = NOW + timedelta(seconds=1) if terminal_status == "CANCELLED" else NOW
    repo.save_decision_batch((item,), occurred_at=item_time)
    persisted_id = intent_id
    repo.update_intent(
        persisted_id,
        state=terminal_status,
        occurred_at=NOW + timedelta(seconds=1),
        broker_order_id=exact_state.broker_order_id,
        requested_amount=exact_state.requested_amount,
        executed_amount=exact_state.executed_amount,
        estimated_commission=exact_state.estimated_commission,
        executed_commission=exact_state.executed_commission,
        executed_lots=exact_state.executed_lots,
        executed_price=exact_state.executed_price,
        executed_at=exact_state.executed_at,
    )
    engine.dispose()

    reopened_engine = create_worker_engine(database_url)
    reopened_factory = sessionmaker(reopened_engine, expire_on_commit=False)
    reopened = LocalAutomationRepository(
        reopened_factory,
        fact_writer=FactOutboxWriter(clock=lambda: NOW + timedelta(seconds=3)),
        environment="PROD",
    )
    broker = ExactStateBroker(exact_state, expected_position)
    service = UncertainIntentReconciliationService(
        reopened,
        broker,
        now=lambda: NOW + timedelta(seconds=3),
        environment="PROD",
    )

    first = asyncio.run(service.reconcile((command_value,)))
    facts_after_first = reopened.ready_fact_outbox(100, now=NOW + timedelta(seconds=4), deadline_ms=0)
    second = asyncio.run(service.reconcile((command_value,)))
    facts_after_second = reopened.ready_fact_outbox(100, now=NOW + timedelta(seconds=5), deadline_ms=0)
    execution_facts = [fact for fact in facts_after_first if fact.fact_kind == "TRADE_EXECUTION_RECORDED"]
    lots = reopened.list_open_lots(str(command_value.automation_id))

    assert first.resolved == 1
    assert second.resolved == 0
    assert broker.request_lookups == 1
    assert facts_after_second == facts_after_first
    expected_side = "BUY" if terminal_status == "FILLED" else "SELL"
    target_execution_facts = [fact for fact in execution_facts if fact.payload["side"] == expected_side]
    assert len(target_execution_facts) == 1
    assert target_execution_facts[0].payload["broker_commission"] == "0.25"
    assert sum(lot.remaining_lots for lot in lots) == expected_position
    assert reopened.get_latest_intent(str(command_value.automation_id)).terminal_at is not None
    reopened_engine.dispose()


def rejected_order_state() -> BrokerOrderState:
    return BrokerOrderState(
        "broker-order",
        "intent",
        "REJECTED",
        1,
        0,
        Decimal("253.08"),
        Decimal(),
        Decimal("0.12654"),
        Decimal(),
        "RUB",
    )


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
                raise BrokerOperationError("BROKER_RATE_LIMITED", "Rate limited.", retryable=True)
            return self.operations

    async def scenario():
        repository = Repository(ledger_lots=5)
        broker = RateLimitedOnceBroker()
        service = UncertainIntentReconciliationService(
            repository,
            broker,
            now=lambda: NOW,
        )
        with pytest.raises(BrokerOperationError):
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
        operation = operation.model_copy(
            update={"occurred_at": CREATED_AT + timedelta(milliseconds=151, microseconds=456)}
        )
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
            raise BrokerOperationError("BROKER_RATE_LIMITED", "Rate limited.", retryable=True)

    async def scenario():
        service = UncertainIntentReconciliationService(repository, UnavailableBroker(()), now=lambda: NOW, audit=audit)
        for _ in range(2):
            with pytest.raises(BrokerOperationError):
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


def test_delayed_fill_is_reconciled_after_cooldown_without_restart() -> None:
    async def scenario():
        repository = Repository(ledger_lots=6)
        broker = Broker((), position_lots="6")
        clock = [NOW]
        service = UncertainIntentReconciliationService(repository, broker, now=lambda: clock[0])
        first = await service.reconcile((command(),))
        broker.exact_state = filled_order_state()
        broker.position_lots = "5"
        immediate = await service.reconcile((command(),))
        clock[0] += timedelta(seconds=60)
        later = await service.reconcile((command(),))
        return repository, first, immediate, later

    repository, first, immediate, later = asyncio.run(scenario())
    assert first.unresolved == 1
    assert immediate.resolved == 0
    assert later.resolved == 1
    assert len(repository.finalizations) == 1
    assert repository.finalizations[0][0].executed_lots == 1
    assert repository.finalizations[0][1] is False


@pytest.mark.parametrize("quantity_units", [10, 9, 1])
def test_recovery_matches_instrument_units_instead_of_lots(quantity_units):
    async def scenario():
        repository = Repository(ledger_lots=6)
        operation = sell_operation().model_copy(update={"quantity_units": Decimal(quantity_units)})
        service = UncertainIntentReconciliationService(repository, Broker((operation,)), now=lambda: NOW)
        result = await service.reconcile((command().model_copy(update={"lot_size": 10}),))
        assert result.resolved == (1 if quantity_units == 10 else 0)
        assert len(repository.finalizations) == result.resolved
        assert result.unresolved == (0 if quantity_units == 10 else 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("environment", ["TEST", "PROD"])
@pytest.mark.parametrize("state", ["FAILED", "ACCEPTED", "PARTIALLY_FILLED"])
@pytest.mark.parametrize("observation", ["missing", "nonterminal", "position_mismatch"])
def test_known_open_ambiguous_result_holds_and_keeps_same_key_and_reservation(
    tmp_path, environment, state, observation
):
    class MissingBroker(Broker):
        keys = []

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.keys.append(idempotency_key)
            if observation == "nonterminal":
                return rejected_order_state().model_copy(update={"status": "SUBMITTED"})
            if observation == "position_mismatch":
                return filled_order_state()

        async def get_order_state(self, account_id, broker_order_id):
            raise AssertionError("Known open recovery must use the stable request key")

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            raise AssertionError("Known open recovery must not infer a broker result from operations")

    engine = create_worker_engine(f"sqlite:///{tmp_path / 'failed.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, environment=environment)
    value = command()
    repo.cache_command(value)
    key = repo.save_decision_batch((decision_item(value),), occurred_at=NOW).intents[0].idempotency_key
    with factory.begin() as session:
        row = session.get(LocalIntentModel, key)
        row.state = state
        row.broker_order_id = "historical-order"
    broker = MissingBroker(())

    async def scenario():
        cash = AccountCashReservationService()
        await cash.restore(tuple(repo.list_active_buy_intent_reservations()))
        reserved = await cash.reserved(value.account_id, "RUB")
        assert reserved > 0
        recovery = UncertainIntentReconciliationService(
            repo, broker, now=lambda: NOW, environment=environment, cash=cash
        )
        result = await recovery.reconcile((value,))
        assert result.unresolved == 1
        assert await cash.reserved(value.account_id, "RUB") == reserved

    asyncio.run(scenario())
    assert broker.keys == [key]
    expected_state = "FAILED" if state == "FAILED" else "UNCERTAIN"
    assert repo.get_active_intent(str(value.automation_id)).state == expected_state
    assert repo.get_state(str(value.automation_id)).state == "HOLD"
    assert repo.list_open_lots(str(value.automation_id)) == []
    facts = repo.ready_fact_outbox(100, now=NOW + timedelta(seconds=1), deadline_ms=0)
    assert not any(fact.fact_kind == "TRADE_EXECUTION_RECORDED" for fact in facts)
    engine.dispose()


@pytest.mark.parametrize(
    ("open_state", "terminal_state", "executed_lots"),
    [("ACCEPTED", "REJECTED", 0), ("PARTIALLY_FILLED", "CANCELLED", 1), ("PARTIALLY_FILLED", "FILLED", 2)],
)
@pytest.mark.parametrize("restart", [False, True])
def test_test_failed_open_observation_reaches_terminal_after_delay_or_restart(
    tmp_path, open_state, terminal_state, executed_lots, restart
):
    value = command()
    automation_id = str(value.automation_id)
    database_url = f"sqlite:///{tmp_path / 'failed-open.db'}"
    clock = [NOW]

    class RequestBroker(Broker):
        def __init__(self):
            super().__init__((), position_lots="0")
            self.keys = []

        async def find_by_idempotency_key(self, account_id, idempotency_key):
            self.keys.append(idempotency_key)
            return self.exact_state

        async def get_order_state(self, account_id, broker_order_id):
            raise AssertionError("Known open recovery must keep the stable request key")

        async def inspect_recent_operations(self, account_id, instrument_id, limit):
            raise AssertionError("Known open recovery must not infer fills from operations")

        async def post_order(self, *args, **kwargs):
            raise AssertionError("Recovery must never repeat submission")

    def open_repository():
        engine = create_worker_engine(database_url)
        initialize_worker_schema(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: clock[0]))
        return engine, factory, repo

    engine, factory, repo = open_repository()
    try:
        repo.cache_command(value)
        item = decision_item(value)
        item = item.model_copy(
            update={"intent": item.intent.model_copy(update={"quantity_lots": 2}), "decision_quantity_lots": 2}
        )
        key = repo.save_decision_batch((item,), occurred_at=NOW).intents[0].idempotency_key
        with factory.begin() as session:
            row = session.get(LocalIntentModel, key)
            row.state = "FAILED"
            row.broker_order_id = "historical-order"
        broker = RequestBroker()
        open_lots = 1 if open_state == "PARTIALLY_FILLED" else 0
        broker.exact_state = BrokerOrderState(
            "broker-order",
            key,
            open_state,
            2,
            open_lots,
            Decimal("200"),
            Decimal(100 * open_lots),
            Decimal("1"),
            Decimal("0.25") if open_lots else Decimal(),
            "RUB",
            executed_price=Decimal("100") if open_lots else Decimal(),
            executed_at=NOW if open_lots else None,
        )
        broker.position_lots = str(open_lots)

        async def scenario():
            nonlocal engine, repo
            cash = AccountCashReservationService()
            await cash.restore(tuple(repo.list_active_buy_intent_reservations()))
            reserved = await cash.reserved(value.account_id, "RUB")
            assert reserved == Decimal("201")
            gate = ActiveIntentGateService()
            gate.activate(automation_id, key)
            recovery = UncertainIntentReconciliationService(
                repo, broker, now=lambda: clock[0], cash=cash, active_intents=gate
            )
            pending = await recovery.reconcile((value,))
            assert pending.resolved == pending.unresolved == 0
            assert repo.get_active_intent(automation_id).state == open_state
            assert repo.list_open_lots(automation_id) == []
            assert await cash.reserved(value.account_id, "RUB") == reserved
            assert gate.has_active_intent(automation_id)
            await recovery.reconcile((value,))
            assert broker.keys == [key]
            clock[0] += timedelta(seconds=11)
            if restart:
                engine.dispose()
                engine, _, repo = open_repository()
                cash = AccountCashReservationService()
                await cash.restore(tuple(repo.list_active_buy_intent_reservations()))
                assert await cash.reserved(value.account_id, "RUB") == reserved
                gate = ActiveIntentGateService()
                gate.activate(automation_id, key)
                recovery = UncertainIntentReconciliationService(
                    repo, broker, now=lambda: clock[0], cash=cash, active_intents=gate
                )
            broker.exact_state = broker.exact_state.model_copy(
                update={
                    "status": terminal_state,
                    "executed_lots": executed_lots,
                    "executed_amount": Decimal(100 * executed_lots),
                    "executed_commission": Decimal("0.25") if executed_lots else Decimal(),
                    "executed_price": Decimal("100") if executed_lots else Decimal(),
                    "executed_at": clock[0] if executed_lots else None,
                }
            )
            broker.position_lots = str(executed_lots)
            result = await recovery.reconcile((value,))
            assert result.resolved == 1
            assert result.unresolved == 0
            assert await cash.reserved(value.account_id, "RUB") == 0
            assert not gate.has_active_intent(automation_id)
            assert repo.get_active_intent(automation_id) is None
            terminal = repo.get_latest_intent(automation_id)
            assert terminal.state == terminal_state
            assert terminal.terminal_at == clock[0]
            assert terminal.executed_lots == executed_lots
            assert sum(lot.remaining_lots for lot in repo.list_open_lots(automation_id)) == executed_lots
            facts = repo.ready_fact_outbox(100, now=clock[0] + timedelta(seconds=1), deadline_ms=0)
            order_facts = [fact for fact in facts if fact.fact_kind == "BROKER_ORDER_STATE_CHANGED"]
            assert order_facts[-1].payload["to_state"] == terminal_state
            execution_facts = [fact for fact in facts if fact.fact_kind == "TRADE_EXECUTION_RECORDED"]
            assert len(execution_facts) == (1 if executed_lots else 0)
            if execution_facts:
                assert execution_facts[0].payload["executed_lots"] == executed_lots
                assert execution_facts[0].payload["broker_commission"] == "0.25"
            repeated = await recovery.reconcile((value,))
            assert repeated.resolved == repeated.unresolved == 0
            assert repo.ready_fact_outbox(100, now=clock[0] + timedelta(seconds=1), deadline_ms=0) == facts
            assert broker.keys == [key, key]

        asyncio.run(scenario())
    finally:
        engine.dispose()
