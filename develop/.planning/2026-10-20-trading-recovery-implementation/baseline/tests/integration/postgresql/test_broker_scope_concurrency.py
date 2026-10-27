"""A second initial account selection cannot validate against a stale scope."""

from threading import Barrier, Thread

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from moex_sentinel.domain.user_brokers import UserBrokerConstraintError, UserBrokerDraft, UserBrokerState
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


def test_initial_account_selection_locks_scope_before_validation(isolated_postgresql_database_url):
    first_engine = create_database_engine(isolated_postgresql_database_url)
    second_engine = create_database_engine(isolated_postgresql_database_url)
    first_factory = create_session_factory(first_engine)
    second_factory = create_session_factory(second_engine)
    draft = UserBrokerDraft(
        "t_invest", "Scope", "TEST", "sandbox-invest-public-api.tbank.ru:443", {}, None, UserBrokerState.DRAFT
    )
    first_repository = UserBrokerRepository(first_factory)
    second_repository = UserBrokerRepository(second_factory)
    loaded_stale_scope = []
    failures = []
    ready = Barrier(2, timeout=5)

    @event.listens_for(second_engine, "connect")
    def bound_lock_wait(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '300ms'")
        connection.commit()

    @event.listens_for(second_engine, "after_cursor_execute")
    def observe_completed_scope_read(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "user_brokers" in statement:
            loaded_stale_scope.append(True)

    def select_other_account(record_id):
        ready.wait()
        try:
            second_repository.replace(record_id, draft.model_copy(update={"external_account_id": "account-B"}))
        except Exception as error:
            failures.append(error)

    try:
        record = first_repository.create(draft)
        with first_factory.begin() as first:
            selected = first.get(UserBrokerModel, record.id, with_for_update=True)
            selected.external_account_id = "account-A"
            first.flush()
            contender = Thread(target=select_other_account, args=(record.id,))
            contender.start()
            ready.wait()
            contender.join(timeout=5)
            assert not contender.is_alive()
            assert len(failures) == 1
            assert isinstance(failures[0], OperationalError)
            # A blocked UPDATE is too late: the second SELECT must wait before inspecting old None.
            assert loaded_stale_scope == []
        with pytest.raises(UserBrokerConstraintError, match="immutable"):
            second_repository.replace(record.id, draft.model_copy(update={"external_account_id": "account-B"}))
        assert first_repository.get(record.id).external_account_id == "account-A"
    finally:
        first_engine.dispose()
        second_engine.dispose()
