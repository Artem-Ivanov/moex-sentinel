"""Public lifecycle commands cannot overwrite a concurrent revision."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, local

import pytest
from sqlalchemy import event

from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automations import AutomationRepository, RevisionConflictError
from sentinel_contracts.trading import AutomationState
from tests.storage.test_automation_command_repository import AUTOMATION_ID, repository
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model


def test_public_state_write_rejects_stale_revision() -> None:
    _, factory = repository()
    repo = AutomationRepository(factory)
    first = repo.set_state(AUTOMATION_ID, AutomationState.HOLD, expected_revision=1)

    with pytest.raises(RevisionConflictError):
        repo.set_state(AUTOMATION_ID, AutomationState.CLOSED, expected_revision=1)

    assert repo.get(AUTOMATION_ID) == first


def test_concurrent_public_commands_have_only_one_revision_winner(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lifecycle-race.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(user_broker_model("scope-1", "synthetic-account"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
        session.flush()
        session.add(automation_model("automation-1", user_broker_id="scope-1", instrument_id="instrument-1"))
    repo = AutomationRepository(factory)
    readers = Barrier(2)
    thread_state = local()

    def synchronize_initial_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if (
            statement.startswith("SELECT")
            and "FROM trading_automations" in statement
            and not getattr(thread_state, "has_read", False)
        ):
            thread_state.has_read = True
            readers.wait(timeout=5)

    def change(state: AutomationState) -> str:
        try:
            repo.set_state("automation-1", state, expected_revision=1)
        except RevisionConflictError:
            return "conflict"
        return "accepted"

    event.listen(engine, "after_cursor_execute", synchronize_initial_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, [AutomationState.HOLD, AutomationState.HOLD]))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_initial_read)

    assert sorted(results) == ["accepted", "conflict"]
    assert repo.get("automation-1").revision == 2
    engine.dispose()
