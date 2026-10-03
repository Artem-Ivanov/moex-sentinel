"""PostgreSQL archive migration and concurrent stale mutation guards."""

from threading import Event, Thread

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from alembic import command
from moex_sentinel.domain.user_brokers import UserBrokerNotFoundError, UserBrokerState
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import UserBrokerModel
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.storage.schema_revision import current_schema_revision
from sentinel_contracts.time import utc_now_ms
from tests.integration.postgresql.test_portfolio_snapshot_schema_migration import (
    _wait_for_access_exclusive_lock,
    unmigrated_postgresql_database_url,  # noqa: F401 - shared isolated schema fixture
)
from tests.migrations.test_user_broker_archive import (
    PREVIOUS,
    REVISION,
    check_populated_archive_migration,
    migration_config,
)
from tests.storage.test_user_broker_repository import draft

pytestmark = pytest.mark.postgresql


def test_populated_postgresql_archive_migration_preserves_history(request) -> None:
    check_populated_archive_migration(request.getfixturevalue("unmigrated_postgresql_database_url"))


@pytest.mark.parametrize("operation", ["archive", "replace"])
def test_archive_lock_prevents_stale_mutation(isolated_postgresql_database_url, operation) -> None:
    first_engine = create_database_engine(isolated_postgresql_database_url)
    second_engine = create_database_engine(isolated_postgresql_database_url)
    first_factory = create_session_factory(first_engine)
    first_repository = UserBrokerRepository(first_factory)
    second_repository = UserBrokerRepository(create_session_factory(second_engine))
    completed_reads = []
    failures = []
    competing_read_started = Event()
    thread = None

    @event.listens_for(second_engine, "connect")
    def bound_wait(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '300ms'")
        connection.commit()

    @event.listens_for(second_engine, "after_cursor_execute")
    def observe_read(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "user_brokers" in statement:
            completed_reads.append(True)

    @event.listens_for(second_engine, "before_cursor_execute")
    def observe_competing_read(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "user_brokers" in statement:
            competing_read_started.set()

    def mutate(record_id):
        try:
            if operation == "archive":
                second_repository.archive(record_id, expected_environment="TEST")
            else:
                second_repository.replace(record_id, draft(), expected_environment="TEST")
        except Exception as error:
            failures.append(error)

    try:
        with second_engine.connect() as connection:
            assert connection.scalar(text("SHOW statement_timeout")) == "300ms"
        created = first_repository.create(draft())
        with first_factory.begin() as session:
            model = session.get(UserBrokerModel, created.id, with_for_update=True)
            model.archived_at = utc_now_ms()
            model.state = UserBrokerState.DISABLED.value
            session.flush()
            thread = Thread(target=mutate, args=(created.id,))
            thread.start()
            assert competing_read_started.wait(timeout=5)
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert len(failures) == 1
            assert isinstance(failures[0], OperationalError)
            assert completed_reads == []
        archived = first_repository.get(created.id)
        assert archived.archived_at is not None
        if operation == "archive":
            assert second_repository.archive(created.id, expected_environment="TEST") == archived
        else:
            with pytest.raises(UserBrokerNotFoundError):
                second_repository.replace(created.id, draft(), expected_environment="TEST")
        assert first_repository.get(created.id) == archived
    finally:
        try:
            if thread is not None:
                thread.join(timeout=5)
                assert not thread.is_alive()
        finally:
            second_engine.dispose()
            first_engine.dispose()


def test_downgrade_locks_broker_table_before_archive_check(isolated_postgresql_database_url) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    repository = UserBrokerRepository(create_session_factory(engine))
    created = repository.create(draft())
    failures = []

    def downgrade():
        try:
            command.downgrade(migration_config(isolated_postgresql_database_url), PREVIOUS)
        except Exception as error:
            failures.append(error)

    try:
        with engine.connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(text("LOCK TABLE user_brokers IN ACCESS SHARE MODE"))
            thread = Thread(target=downgrade)
            thread.start()
            try:
                _wait_for_access_exclusive_lock(engine, "user_brokers")
                blocker.execute(
                    text("UPDATE user_brokers SET archived_at = :moment, state = 'DISABLED' WHERE id = :id"),
                    {"moment": utc_now_ms(), "id": created.id},
                )
            finally:
                transaction.commit()
                thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert "archived" in str(failures[0])
        assert current_schema_revision(engine) == REVISION
        assert repository.get(created.id).archived_at is not None
    finally:
        engine.dispose()
