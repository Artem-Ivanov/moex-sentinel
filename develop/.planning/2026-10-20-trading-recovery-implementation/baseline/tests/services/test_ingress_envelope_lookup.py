"""Combined lookup preserves replay, conflict priority and group atomicity."""

from uuid import UUID

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.storage.models import AutomationEventModel, TradingAutomationModel
from sentinel_contracts.trading_facts import FactIngressErrorCode
from tests.services.test_trading_fact_ingress import (
    AUTOMATION_A,
    AUTOMATION_B,
    complete_fact_sequence,
    service,
    state_fact,
)
from tests.services.test_trading_fact_ingress import database as _shared_database

database = _shared_database
EVENT_A = UUID("00000000-0000-4000-8000-000000000701")
EVENT_B = UUID("00000000-0000-4000-8000-000000000702")


def test_exact_replay_uses_one_lookup_and_never_reapplies(database: tuple[Engine, sessionmaker[Session]]) -> None:
    engine, factory = database
    fact = state_fact(AUTOMATION_A, event_id=EVENT_A)
    first = service(factory).publish([fact])
    assert first.failures == ()
    statements: list[str] = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        replay = service(factory).publish([fact])
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert replay == first
    # One automation read and one envelope lookup; replay performs no writes.
    assert len(statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_duplicate_inside_group_is_acknowledged_without_second_mutation(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    fact = state_fact(AUTOMATION_A, event_id=EVENT_A)

    result = service(factory).publish([fact, fact])

    assert result.failures == ()
    assert result.results[0].accepted_event_ids == (EVENT_A, EVENT_A)
    assert result.results[0].current_revision == 2
    assert result.results[0].accepted_through_sequence == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 1


@pytest.mark.parametrize("other_automation", [False, True])
def test_event_conflict_has_priority_over_sequence_conflict(
    database: tuple[Engine, sessionmaker[Session]], other_automation: bool
) -> None:
    _, factory = database
    if other_automation:
        first = state_fact(AUTOMATION_A, event_id=EVENT_A)
        second = state_fact(AUTOMATION_B, event_id=EVENT_B)
    else:
        first, second = complete_fact_sequence()[:2]
    assert service(factory).publish([first, second]).failures == ()
    # Its event identifies the first stored row; its sequence identifies the second.
    conflict = second.model_copy(update={"event_id": first.event_id})

    result = service(factory).publish([conflict])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.FACT_ID_CONFLICT
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 2


def test_old_replay_and_new_fact_advance_only_once(database: tuple[Engine, sessionmaker[Session]]) -> None:
    _, factory = database
    first, second = complete_fact_sequence()[:2]
    assert service(factory).publish([first]).failures == ()

    result = service(factory).publish([first, second])

    assert result.failures == ()
    assert result.results[0].accepted_event_ids == (first.event_id, second.event_id)
    assert result.results[0].accepted_through_sequence == 2
    assert result.results[0].current_revision == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 2


def test_same_sequence_collision_rolls_back_group_but_keeps_successful_peer(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    first = state_fact(AUTOMATION_A, event_id=EVENT_A)
    collision = first.model_copy(update={"event_id": EVENT_B})
    peer = state_fact(AUTOMATION_B, event_id=UUID("00000000-0000-4000-8000-000000000703"))

    result = service(factory).publish([first, collision, peer])

    assert len(result.failures) == len(result.results) == 1
    assert result.failures[0].code is FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT
    assert result.results[0].automation_id == AUTOMATION_B
    with factory() as session:
        unchanged = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (unchanged.state, unchanged.revision, unchanged.last_sequence_number) == ("IN_WORK", 1, 0)
        assert list(session.scalars(select(AutomationEventModel.event_id))) == [str(peer.event_id)]
