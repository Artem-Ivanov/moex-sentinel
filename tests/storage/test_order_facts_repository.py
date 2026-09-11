"""Baseline order facts repository tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    BrokerOrderDraft,
    BrokerOrderEventDraft,
    BrokerOrderStatus,
    TradeDecisionDraft,
    TradeExecutionDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.order_facts import OrderFactsRepository
from tests.domain.test_trading_facts import all_fact_drafts
from tests.storage.test_trading_facts_models import automation_model, seed_cycle


@pytest.fixture
def database() -> Iterator[tuple[Engine, Session]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_cycle(session)
        yield engine, session
    engine.dispose()


def fact_value(value_type):
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


def test_exact_fact_retry_returns_existing_detached_value(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)

    first = repository.append_decision("scope-1", decision)
    second = repository.append_decision("scope-1", decision.model_copy(deep=True))

    assert second == first == decision
    first.indicators["caller-change"] = True
    assert "caller-change" not in repository.get_decision("scope-1", decision.id).indicators


def test_conflicting_fact_retry_raises_stable_error(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    repository.append_decision("scope-1", decision)
    conflict = decision.model_copy(update={"id": "decision-2", "reason_code": "DIFFERENT"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_decision("scope-1", conflict)

    assert caught.value.code is TradingFactErrorCode.FACT_ID_CONFLICT


def test_order_lineage_and_history_are_scoped_and_ordered(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    order = fact_value(BrokerOrderDraft)
    event = fact_value(BrokerOrderEventDraft)
    execution = fact_value(TradeExecutionDraft)
    repository.append_decision("scope-1", decision)
    repository.append_order("scope-1", order)
    repository.append_order_event("scope-1", event)
    repository.append_execution("scope-1", execution)

    assert repository.get_order("scope-1", order.id) == order
    assert repository.list_order_events("scope-1", order.id) == (event,)
    assert repository.list_executions("scope-1", order.id) == (execution,)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.get_order("scope-2", order.id)
    assert caught.value.code is TradingFactErrorCode.NOT_FOUND


def test_repository_rejects_cross_scope_value_before_flush(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft).model_copy(update={"user_broker_id": "scope-2"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_decision("scope-1", decision)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE


def test_repository_rejects_cross_scope_parent_lineage_before_flush(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    repository.append_decision("scope-1", decision)
    cross_scope = fact_value(BrokerOrderDraft).model_copy(update={"user_broker_id": "scope-2"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_order("scope-2", cross_scope)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE


def test_repository_rejects_same_scope_cross_automation_cycle_before_flush(
    database: tuple[Engine, Session],
) -> None:
    _, session = database
    session.add(automation_model("automation-2", state="CLOSED", closed=True))
    session.flush()
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft).model_copy(
        update={
            "id": "decision-2",
            "fact_id": "fact-decision-2",
            "automation_id": "automation-2",
        }
    )

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_decision("scope-1", decision)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE


def test_repository_translates_one_order_per_decision_constraint(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    order = fact_value(BrokerOrderDraft)
    repository.append_decision("scope-1", decision)
    repository.append_order("scope-1", order)
    duplicate = order.model_copy(
        update={
            "id": "order-2",
            "fact_id": "fact-order-2",
            "idempotency_key": "idempotency-2",
        }
    )

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_order("scope-1", duplicate)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE


def test_order_aggregate_replacement_preserves_identity_and_lineage(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    order = fact_value(BrokerOrderDraft)
    repository.append_decision("scope-1", decision)
    repository.append_order("scope-1", order)
    changed = order.model_copy(
        update={
            "external_order_id": "synthetic-external-order",
            "state": BrokerOrderStatus.SUBMITTED,
            "requested_amount": order.requested_amount + 1,
            "executed_amount": order.requested_amount,
            "estimated_commission": order.estimated_commission + 1,
        }
    )

    saved = repository.replace_order_aggregate("scope-1", changed)

    assert saved.id == order.id
    assert saved.fact_id == order.fact_id
    assert saved.decision_id == order.decision_id
    assert saved.idempotency_key == order.idempotency_key
    assert saved.external_order_id == "synthetic-external-order"
    assert saved.state.value == "SUBMITTED"
    assert saved.requested_amount == order.requested_amount + 1
    assert saved.executed_amount == order.requested_amount
    assert saved.estimated_commission == order.estimated_commission + 1


def test_order_aggregate_replacement_rejects_changed_lineage_without_mutation(
    database: tuple[Engine, Session],
) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    order = fact_value(BrokerOrderDraft)
    repository.append_decision("scope-1", decision)
    repository.append_order("scope-1", order)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.replace_order_aggregate("scope-1", order.model_copy(update={"automation_id": "other"}))

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert repository.get_order("scope-1", order.id) == order


def test_order_aggregate_replacement_rejects_changed_intent_without_mutation(
    database: tuple[Engine, Session],
) -> None:
    _, session = database
    repository = OrderFactsRepository(session)
    decision = fact_value(TradeDecisionDraft)
    order = fact_value(BrokerOrderDraft)
    repository.append_decision("scope-1", decision)
    repository.append_order("scope-1", order)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.replace_order_aggregate("scope-1", order.model_copy(update={"quantity_lots": 2}))

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert repository.get_order("scope-1", order.id) == order
