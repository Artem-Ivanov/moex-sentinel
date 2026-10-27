"""Typed state boundaries reject typos before they can hide durable work."""

import asyncio
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sentinel_contracts.broker_execution import BrokerOrderState
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import NOW, baseline_command, filled_buy
from trading_automaton.domain.storage_dtos import LocalAutomationRecord
from trading_automaton.services.account_cash_reservation import AccountCashReservationService
from trading_automaton.storage.models import LocalIntentModel


def test_automation_record_rejects_unknown_state():
    with pytest.raises(ValidationError):
        LocalAutomationRecord("id", "TYPO", 1, 0)


def test_broker_order_rejects_unknown_state():
    with pytest.raises(ValidationError):
        BrokerOrderState("broker", "key", "TYPO", 1, 0, 100, 0, 0, 0, "RUB")


def test_repository_rejects_order_regression_without_fact(worker_repository_factory):
    repo, _ = worker_repository_factory()
    command = baseline_command()
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    repo.update_intent(intent.idempotency_key, state="ACCEPTED", occurred_at=NOW)
    before = repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)
    with pytest.raises(ValueError, match="transition"):
        repo.update_intent(intent.idempotency_key, state="SUBMITTING", occurred_at=NOW)
    assert repo.get_active_intent(str(command.automation_id)).state == "ACCEPTED"
    assert repo.ready_fact_outbox(100, now=NOW, deadline_ms=0) == before


def test_finalized_same_state_cannot_rewrite_financial_result(worker_repository_factory):

    repo, _ = worker_repository_factory()
    command = baseline_command()
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    repo.finalize_execution(filled_buy())
    with pytest.raises(ValueError, match="terminal"):
        repo.update_intent(intent.idempotency_key, state="FILLED", executed_amount=Decimal(42), occurred_at=NOW)


def test_legacy_failed_blocks_new_intent_and_retains_cash(worker_repository_factory):

    repo, factory = worker_repository_factory()
    command = baseline_command()
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    with factory.begin() as session:
        session.get(LocalIntentModel, intent.idempotency_key).state = "FAILED"
    assert repo.get_active_intent(str(command.automation_id)).state == "FAILED"
    assert repo.has_active_intents()
    reservations = tuple(repo.list_active_buy_intent_reservations())
    assert len(reservations) == 1

    async def check():
        cash = AccountCashReservationService()
        await cash.replace_snapshot(command.account_id, "RUB", Decimal(2000))
        await cash.restore(reservations)
        before = await cash.reserved(command.account_id, "RUB")
        assert before > 0
        await cash.complete(intent.idempotency_key, "FAILED")
        assert await cash.reserved(command.account_id, "RUB") == before

    asyncio.run(check())


@pytest.mark.parametrize(
    "module", ["trading_automaton.adapters.tinvest_broker_session", "moex_sentinel.adapters.tinvest.order_execution"]
)
def test_unknown_sdk_status_is_uncertain_observation(module):

    money = SimpleNamespace(units=0, nano=0, currency="rub")
    response = SimpleNamespace(
        execution_report_status="FUTURE_SDK_STATUS",
        initial_order_price=money,
        total_order_amount=money,
        initial_commission=money,
        executed_commission=money,
        executed_order_price=money,
        order_id="broker-order",
        order_request_id="same-key",
        lots_requested=1,
        lots_executed=0,
    )
    result = import_module(module)._order_state(response)
    assert result.status == "UNCERTAIN"
    assert result.idempotency_key == "same-key"


def test_persisted_dtos_reject_typos_in_order_and_outbox(worker_repository_factory):
    repo, _ = worker_repository_factory()
    command = baseline_command()
    repo.cache_command(command)
    intent = repo.save_decision_batch((decision_item(command),), occurred_at=NOW).intents[0]
    outbox = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)[0]
    for record, field in ((intent, "state"), (outbox, "delivery_state")):
        with pytest.raises(ValidationError, match=field):
            type(record).model_validate({**record.model_dump(), field: "TYPO"})
