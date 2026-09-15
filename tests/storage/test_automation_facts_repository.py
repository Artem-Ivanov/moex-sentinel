"""Baseline automation repository tests."""

from collections.abc import Iterator
from typing import TypeVar

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automation_facts import AutomationFactsRepository
from sentinel_contracts.trading import AutomationState
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

FactDraftT = TypeVar("FactDraftT", bound=TradingAutomationDraft)


@pytest.fixture
def database() -> Iterator[tuple[Engine, Session]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(user_broker_model("scope-1", "account-1"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
        session.flush()
        yield engine, session
    engine.dispose()


def fact_value(value_type: type[FactDraftT]) -> FactDraftT:
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


def test_create_persists_strategy_free_automation(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    created = repository.create("scope-1", automation)

    assert created == automation
    assert repository.get("scope-1", automation.id) == automation


def test_compare_and_set_state_rejects_stale_revision(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    repository.create("scope-1", automation)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.compare_and_set_state(
            "scope-1",
            automation.id,
            expected_revision=7,
            state=AutomationState.HOLD,
            hold_reason="synthetic hold",
            closed_at=None,
        )

    assert caught.value.code is TradingFactErrorCode.REVISION_CONFLICT


def test_accept_supporting_fact_advances_only_sequence(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    repository.create("scope-1", automation)

    accepted = repository.accept_supporting_fact(
        "scope-1",
        automation.id,
        expected_revision=1,
        expected_sequence=0,
        sequence_number=1,
    )

    assert accepted.revision == 1
    assert accepted.last_sequence_number == 1


def test_accept_state_fact_advances_revision_and_sequence_together(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    repository.create("scope-1", automation)

    accepted = repository.accept_state_fact(
        "scope-1",
        automation.id,
        expected_revision=1,
        expected_sequence=0,
        sequence_number=1,
        state=AutomationState.HOLD,
        suspended_from_state=AutomationState.IN_WORK,
        hold_reason="synthetic hold",
        closed_at=None,
    )

    assert accepted.state is AutomationState.HOLD
    assert accepted.suspended_from_state is AutomationState.IN_WORK
    assert accepted.revision == 2
    assert accepted.last_sequence_number == 1


def test_accept_fact_rejects_stale_sequence_without_mutation(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    repository.create("scope-1", automation)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.accept_supporting_fact(
            "scope-1",
            automation.id,
            expected_revision=1,
            expected_sequence=1,
            sequence_number=2,
        )

    assert caught.value.code is TradingFactErrorCode.SEQUENCE_CONFLICT
    assert repository.get("scope-1", automation.id).last_sequence_number == 0


def test_automation_reads_are_scoped(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = AutomationFactsRepository(session)
    automation = fact_value(TradingAutomationDraft)
    repository.create("scope-1", automation)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.get("scope-2", automation.id)

    assert caught.value.code is TradingFactErrorCode.NOT_FOUND
