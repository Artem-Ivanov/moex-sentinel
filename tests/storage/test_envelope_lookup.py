"""Scope, identity and query-budget contract shared with PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import AutomationEnvelopeDraft
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository
from tests.storage.test_trading_observability_repository import fact_value
from tests.storage.trading_facts_helpers import automation_model, seed_two_scopes


@pytest.fixture
def envelope_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def envelope_session(envelope_engine: Engine) -> Iterator[Session]:
    with Session(envelope_engine) as session:
        seed_two_scopes(session)
        session.add_all(
            [
                automation_model("automation-1"),
                automation_model("automation-2", state="CLOSED", closed=True),
                automation_model("automation-3", user_broker_id="scope-2", instrument_id="instrument-2"),
            ]
        )
        session.flush()
        template = fact_value(AutomationEnvelopeDraft)
        for event_id, scope, automation, sequence in (
            ("event-1", "scope-1", "automation-1", 1),
            ("event-2", "scope-1", "automation-1", 2),
            ("event-3", "scope-1", "automation-2", 1),
            ("event-4", "scope-2", "automation-3", 1),
        ):
            TradingAuditRepository(session).append_envelope(
                scope,
                template.model_copy(
                    update={
                        "event_id": event_id,
                        "user_broker_id": scope,
                        "automation_id": automation,
                        "sequence_number": sequence,
                    }
                ),
            )
        yield session


class TestEnvelopeLookup:
    @pytest.mark.parametrize(
        ("scope", "event_id", "automation", "sequence", "expected"),
        [
            ("scope-1", "missing", "automation-1", 3, set()),
            ("scope-1", "event-1", "automation-1", 3, {"event-1"}),
            ("scope-1", "missing", "automation-1", 1, {"event-1"}),
            ("scope-1", "event-1", "automation-1", 1, {"event-1"}),
            ("scope-1", "event-1", "automation-1", 2, {"event-1", "event-2"}),
            ("scope-1", "event-3", "automation-1", 1, {"event-1", "event-3"}),
            ("scope-1", "event-4", "automation-3", 1, set()),
            ("scope-1", "event-4", "automation-1", 1, {"event-1"}),
            ("scope-2", "event-1", "automation-1", 1, set()),
            ("scope-1", "missing", "automation-1", 2, {"event-2"}),
        ],
    )
    def test_matches_both_keys_with_one_query(
        self,
        envelope_engine: Engine,
        envelope_session: Session,
        scope: str,
        event_id: str,
        automation: str,
        sequence: int,
        expected: set[str],
    ) -> None:
        statements: list[str] = []

        def record_statement(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(envelope_engine, "before_cursor_execute", record_statement)
        try:
            matches = TradingAuditRepository(envelope_session).find_envelope_matches(
                scope, event_id, automation, sequence
            )
        finally:
            event.remove(envelope_engine, "before_cursor_execute", record_statement)

        assert len(statements) == 1
        assert {value.event_id for value in matches} == expected
        assert len(matches) == len(expected)
        assert all(value.user_broker_id == scope for value in matches)
        template = fact_value(AutomationEnvelopeDraft)
        assert all(value.payload == template.payload for value in matches)
        assert all(value.occurred_at == template.occurred_at for value in matches)
        assert all(value.received_at == template.received_at for value in matches)
