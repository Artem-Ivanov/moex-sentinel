"""Actual Worker outbox order observations traverse Core's mapper and persistence."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from moex_sentinel.storage.models import BrokerOrderEventModel, BrokerOrderModel
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    INSTRUMENT_A,
    NOW,
    SCOPE_ID,
    service,
)
from tests.services.test_trading_fact_ingress import (
    database as _database_fixture,
)
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import baseline_command, filled_buy
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.models import FactOutboxModel, LocalIntentModel

database = _database_fixture


@pytest.mark.parametrize(
    "states",
    [("SUBMITTING", "FILLED", "UNCERTAIN", "FILLED"), ("FILLED", "REJECTED"), ("EXPIRED", "ACCEPTED", "REJECTED")],
)
def test_worker_recovery_order_facts_reach_core(database, worker_repository_factory, states):
    _, factory = database
    repo, _ = worker_repository_factory()
    command = baseline_command().model_copy(
        update={"automation_id": AUTOMATION_A, "user_broker_id": SCOPE_ID, "instrument_id": INSTRUMENT_A}
    )
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    for state in states:
        repo.update_intent(intent.idempotency_key, state=state, occurred_at=NOW)
    facts = [FactSynchronizationService._envelope(row) for row in repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)]
    result = service(factory).publish(facts)
    assert result.failures == ()
    with factory() as session:
        assert session.scalar(select(BrokerOrderModel)).state == states[-1]
        assert session.scalar(select(func.count()).select_from(BrokerOrderEventModel)) == len(states)


@pytest.mark.parametrize("executed_lots", [0, 1])
def test_expired_worker_finalization_preserves_finances_in_core(database, worker_repository_factory, executed_lots):
    _, factory = database
    repo, _ = worker_repository_factory()
    command = baseline_command().model_copy(
        update={"automation_id": AUTOMATION_A, "user_broker_id": SCOPE_ID, "instrument_id": INSTRUMENT_A}
    )
    repo.cache_command(command)
    item = decision_item(command)
    item = item.model_copy(
        update={"intent": item.intent.model_copy(update={"quantity_lots": 2}), "decision_quantity_lots": 2}
    )
    intent = repo.save_decision_batch((item,), occurred_at=NOW).intents[0]
    finalization = filled_buy().model_copy(
        update={
            "intent_id": intent.idempotency_key,
            "automation_id": str(AUTOMATION_A),
            "state": "EXPIRED",
            "quantity_lots": 2,
            "requested_amount": Decimal(2000),
            "executed_lots": executed_lots,
            "executed_amount": Decimal(1000 * executed_lots),
            "executed_price": Decimal(100 * executed_lots),
            "executed_commission": Decimal(2 * executed_lots),
            "executed_at": NOW if executed_lots else None,
        }
    )
    assert repo.finalize_execution(finalization).applied
    facts = [FactSynchronizationService._envelope(row) for row in repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)]
    result = service(factory).publish(facts)
    assert result.failures == ()
    with factory() as session:
        order = session.scalar(select(BrokerOrderModel))
        assert order.state == "EXPIRED"
        assert order.executed_amount == Decimal(1000 * executed_lots)


def test_legacy_failed_correction_outbox_is_accepted_by_core(database, worker_repository_factory):
    _, factory = database
    repo, worker_factory = worker_repository_factory()
    command = baseline_command().model_copy(
        update={"automation_id": AUTOMATION_A, "user_broker_id": SCOPE_ID, "instrument_id": INSTRUMENT_A}
    )
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    # Seed a historical FAILED snapshot; no current writer invents this broker result.
    with worker_factory.begin() as session:
        session.get(LocalIntentModel, intent.idempotency_key).state = "FAILED"
        order_fact = session.scalar(select(FactOutboxModel).where(FactOutboxModel.fact_kind == "BROKER_ORDER_RECORDED"))
        order_fact.payload = {**order_fact.payload, "state": "FAILED"}
    repo.update_intent(intent.idempotency_key, state="ACCEPTED", occurred_at=NOW)
    repo.update_intent(intent.idempotency_key, state="EXPIRED", occurred_at=NOW, terminal_at=NOW)
    facts = [FactSynchronizationService._envelope(row) for row in repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)]
    assert service(factory).publish(facts).failures == ()
    with factory() as session:
        assert session.scalar(select(BrokerOrderModel)).state == "EXPIRED"


def test_worker_terminal_repair_updates_core_without_duplicate_order_event(database, worker_repository_factory):
    _, factory = database
    repo, worker_factory = worker_repository_factory()
    command = baseline_command().model_copy(
        update={"automation_id": AUTOMATION_A, "user_broker_id": SCOPE_ID, "instrument_id": INSTRUMENT_A}
    )
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    repo.update_intent(intent.idempotency_key, state="FILLED", occurred_at=NOW)
    facts = [FactSynchronizationService._envelope(row) for row in repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)]
    first = service(factory).publish(facts)
    assert first.failures == ()
    ack = first.results[0]
    repo.acknowledge_fact_outbox(
        str(AUTOMATION_A),
        accepted_through_sequence=ack.accepted_through_sequence,
        current_revision=ack.current_revision,
    )
    # A retained local recovery snapshot can lag the terminal observation already accepted by Core.
    with worker_factory.begin() as session:
        session.get(LocalIntentModel, intent.idempotency_key).state = "UNCERTAIN"
    finalization = filled_buy().model_copy(update={"automation_id": str(AUTOMATION_A)})
    repo.finalize_execution(finalization)
    repair = [FactSynchronizationService._envelope(row) for row in repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)]
    assert service(factory).publish(repair).failures == ()
    with factory() as session:
        order = session.scalar(select(BrokerOrderModel))
        assert order.terminal_at == finalization.terminal_at
        assert order.executed_amount == Decimal(1000)
        assert session.scalar(select(func.count()).select_from(BrokerOrderEventModel)) == 1
