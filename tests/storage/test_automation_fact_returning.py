"""Acceptance contracts shared by SQLite and PostgreSQL RETURNING tests."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, event, update
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import (
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.repositories.automation_facts import AutomationFactsRepository
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.trading import AutomationState
from tests.storage.test_automation_facts_repository import fact_value
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

OBSERVED_AT = datetime(2026, 8, 13, 12, 34, 56, 123000, tzinfo=UTC)
CLOSED_AT = datetime(2026, 8, 14, 15, 34, 56, 987654, tzinfo=timezone(timedelta(hours=3)))


@pytest.fixture
def automation_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def automation_factory(automation_engine: Engine) -> sessionmaker[Session]:
    factory = create_session_factory(automation_engine)
    with factory.begin() as session:
        session.add(user_broker_model("scope-1", "account-1"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
        session.flush()
        AutomationFactsRepository(session).create("scope-1", fact_value(TradingAutomationDraft))
    return factory


def accept_fact(
    repository: AutomationFactsRepository,
    kind: str,
    *,
    scope: str = "scope-1",
    automation_id: str = "automation-1",
    revision: int = 1,
    sequence: int = 0,
    next_sequence: int = 1,
) -> TradingAutomationDraft:
    if kind == "supporting":
        return repository.accept_supporting_fact(
            scope,
            automation_id,
            expected_revision=revision,
            expected_sequence=sequence,
            sequence_number=next_sequence,
        )
    return repository.accept_state_fact(
        scope,
        automation_id,
        expected_revision=revision,
        expected_sequence=sequence,
        sequence_number=next_sequence,
        state=AutomationState.CLOSED if kind == "closed" else AutomationState.HOLD,
        suspended_from_state=None if kind == "closed" else AutomationState.IN_WORK,
        hold_reason=None if kind == "closed" else "accepted hold",
        closed_at=CLOSED_AT if kind == "closed" else None,
    )


class TestAutomationFactReturning:
    @pytest.mark.parametrize("kind", ["supporting", "state", "closed"])
    def test_accept_refreshes_loaded_aggregate_and_returns_complete_database_values(
        self, automation_factory: sessionmaker[Session], kind: str
    ) -> None:
        with automation_factory.begin() as session:
            loaded = session.get(TradingAutomationModel, "automation-1")
            assert loaded is not None
            # A Core UPDATE changes the stored snapshot without refreshing the held ORM object.
            snapshot = {
                "state": "HOLD",
                "suspended_from_state": "IN_WORK",
                "hold_reason": "stored hold",
                "resume_requested": True,
                "bootstrap_position_cycle_id": "bootstrap-cycle",
                "bootstrap_position_lot_id": "bootstrap-lot",
                "bootstrap_quantity_lots": 3,
                "bootstrap_average_price": Decimal("12.123456789"),
                "bootstrap_invested_amount": Decimal("36.370370367"),
                "bootstrap_currency": "RUB",
                "bootstrap_observed_at": OBSERVED_AT,
            }
            session.execute(
                update(TradingAutomationModel.__table__)
                .where(TradingAutomationModel.id == "automation-1")
                .values(**snapshot)
            )
            assert loaded.resume_requested is False
            accepted = accept_fact(AutomationFactsRepository(session), kind)

            expected = fact_value(TradingAutomationDraft).model_dump()
            expected.update(snapshot)
            expected.update(last_sequence_number=1, updated_at=accepted.updated_at)
            if kind != "supporting":
                expected.update(
                    state="CLOSED" if kind == "closed" else "HOLD",
                    suspended_from_state=None if kind == "closed" else "IN_WORK",
                    hold_reason=None if kind == "closed" else "accepted hold",
                    closed_at=datetime(2026, 8, 14, 12, 34, 56, 987000, tzinfo=UTC) if kind == "closed" else None,
                    revision=2,
                )
            assert accepted == TradingAutomationDraft(**expected)
            assert accepted.updated_at > fact_value(TradingAutomationDraft).updated_at
            assert accepted.updated_at.tzinfo is UTC
            assert accepted.updated_at.microsecond % 1000 == 0
            assert accepted.bootstrap_observed_at.tzinfo is UTC
            for field, value in expected.items():
                assert getattr(loaded, field) == value, field

        with automation_factory() as session:
            assert AutomationFactsRepository(session).get("scope-1", "automation-1") == accepted

    @pytest.mark.parametrize("kind", ["supporting", "state"])
    def test_successive_accepts_in_one_unit_of_work_need_no_followup_reads(
        self, automation_factory: sessionmaker[Session], automation_engine: Engine, kind: str
    ) -> None:
        statements: list[str] = []

        def record_statement(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split(None, 1)[0].upper())

        with TradingFactsUnitOfWork(automation_factory) as uow:
            initial = uow.automations.get("scope-1", "automation-1")
            event.listen(automation_engine, "before_cursor_execute", record_statement)
            try:
                first = accept_fact(uow.automations, kind)
                second = accept_fact(
                    uow.automations,
                    kind,
                    revision=first.revision,
                    sequence=first.last_sequence_number,
                    next_sequence=2,
                )
            finally:
                event.remove(automation_engine, "before_cursor_execute", record_statement)
            assert first.last_sequence_number == 1
            assert second.last_sequence_number == 2
            assert first.revision == (1 if kind == "supporting" else 2)
            assert second.revision == (1 if kind == "supporting" else 3)
            assert second.state == (initial.state if kind == "supporting" else AutomationState.HOLD)
            assert initial.last_sequence_number == 0
            assert first.updated_at.tzinfo is second.updated_at.tzinfo is UTC
            # Supporting performance assertion, after validating returned aggregate behavior.
            assert statements == ["UPDATE", "UPDATE"]

        with automation_factory() as session:
            assert AutomationFactsRepository(session).get("scope-1", "automation-1") == second

    @pytest.mark.parametrize("kind", ["supporting", "state"])
    @pytest.mark.parametrize(
        ("scope", "automation_id", "revision", "sequence", "next_sequence", "code"),
        [
            ("scope-2", "automation-1", 1, 0, 1, TradingFactErrorCode.NOT_FOUND),
            ("scope-1", "absent", 1, 0, 1, TradingFactErrorCode.NOT_FOUND),
            ("scope-1", "automation-1", 2, 0, 1, TradingFactErrorCode.REVISION_CONFLICT),
            ("scope-1", "automation-1", 2, 1, 2, TradingFactErrorCode.REVISION_CONFLICT),
            ("scope-1", "automation-1", 1, 1, 2, TradingFactErrorCode.SEQUENCE_CONFLICT),
            ("scope-1", "automation-1", 1, 0, 2, TradingFactErrorCode.SEQUENCE_CONFLICT),
        ],
    )
    def test_failed_accept_keeps_conflict_classification_and_does_not_mutate(
        self,
        automation_factory: sessionmaker[Session],
        kind: str,
        scope: str,
        automation_id: str,
        revision: int,
        sequence: int,
        next_sequence: int,
        code: TradingFactErrorCode,
    ) -> None:
        with automation_factory.begin() as session:
            repository = AutomationFactsRepository(session)
            with pytest.raises(TradingFactPersistenceError) as caught:
                accept_fact(
                    repository,
                    kind,
                    scope=scope,
                    automation_id=automation_id,
                    revision=revision,
                    sequence=sequence,
                    next_sequence=next_sequence,
                )
            assert caught.value.code is code
            assert repository.get("scope-1", "automation-1") == fact_value(TradingAutomationDraft)

    @pytest.mark.parametrize("kind", ["supporting", "state"])
    @pytest.mark.parametrize(
        ("stored_revision", "code"),
        [(1, TradingFactErrorCode.SEQUENCE_CONFLICT), (2, TradingFactErrorCode.REVISION_CONFLICT)],
    )
    def test_failed_accept_classifies_database_revision_with_stale_loaded_aggregate(
        self,
        automation_factory: sessionmaker[Session],
        kind: str,
        stored_revision: int,
        code: TradingFactErrorCode,
    ) -> None:
        with automation_factory.begin() as session:
            loaded = session.get(TradingAutomationModel, "automation-1")
            assert loaded is not None
            session.execute(
                update(TradingAutomationModel.__table__)
                .where(TradingAutomationModel.id == "automation-1")
                .values(revision=stored_revision, last_sequence_number=1)
            )
            assert loaded.revision == 1
            with pytest.raises(TradingFactPersistenceError) as caught:
                accept_fact(AutomationFactsRepository(session), kind)
            assert caught.value.code is code

        with automation_factory() as session:
            persisted = AutomationFactsRepository(session).get("scope-1", "automation-1")
            assert persisted.revision == stored_revision
            assert persisted.last_sequence_number == 1
            assert persisted.state is AutomationState.IN_WORK

    @pytest.mark.parametrize("kind", ["supporting", "state"])
    def test_later_failed_accept_rolls_back_successful_accepts_in_unit_of_work(
        self, automation_factory: sessionmaker[Session], kind: str
    ) -> None:
        # Both writes belong to the transaction whose rollback is under test.
        with (  # noqa: PT012
            pytest.raises(TradingFactPersistenceError) as caught,
            TradingFactsUnitOfWork(automation_factory) as uow,
        ):
            first = accept_fact(uow.automations, kind)
            assert first.last_sequence_number == 1
            accept_fact(uow.automations, kind, revision=first.revision, sequence=0, next_sequence=1)

        assert caught.value.code is TradingFactErrorCode.SEQUENCE_CONFLICT
        with automation_factory() as session:
            persisted = AutomationFactsRepository(session).get("scope-1", "automation-1")
            assert persisted == fact_value(TradingAutomationDraft)
