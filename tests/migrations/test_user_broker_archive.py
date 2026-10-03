"""Archive migration never rebuilds or discards populated broker history."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.storage.schema_revision import current_schema_revision
from tests.migrations.user_broker_archive_helpers import archive_graph_snapshot, seed_archive_graph
from tests.storage.test_user_broker_repository import draft

PREVIOUS = "0002_portfolio_snapshot_runs"
REVISION = "0003_user_broker_archive"


def migration_config(url) -> Config:
    config = Config("alembic.ini")
    value = url if isinstance(url, str) else url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", value.replace("%", "%%"))
    return config


def broker_indexes(engine) -> list[dict]:
    indexes = inspect(engine).get_indexes("user_brokers")
    for index in indexes:
        index["dialect_options"] = {name: str(value) for name, value in index.get("dialect_options", {}).items()}
    return indexes


def check_populated_archive_migration(url) -> None:
    config = migration_config(url)
    command.upgrade(config, PREVIOUS)
    engine = create_database_engine(url)
    try:
        seed_archive_graph(engine)
        before = archive_graph_snapshot(engine, include_archive=False)
        before_constraints = inspect(engine).get_check_constraints("user_brokers")
        before_indexes = broker_indexes(engine)
        before_fks = {name: inspect(engine).get_foreign_keys(name) for name in before}

        command.upgrade(config, "head")
        command.upgrade(config, "head")

        assert current_schema_revision(engine) == REVISION
        assert archive_graph_snapshot(engine, include_archive=False) == before
        assert inspect(engine).get_check_constraints("user_brokers") == before_constraints
        assert broker_indexes(engine) == before_indexes
        assert {name: inspect(engine).get_foreign_keys(name) for name in before} == before_fks
        assert UserBrokerRepository(create_session_factory(engine)).get("scope-1").archived_at is None
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

        command.downgrade(config, PREVIOUS)
        assert current_schema_revision(engine) == PREVIOUS
        assert archive_graph_snapshot(engine, include_archive=False) == before
        command.upgrade(config, "head")
        archived = UserBrokerRepository(create_session_factory(engine)).archive("scope-1", expected_environment="TEST")
        assert archived.archived_at is not None
        after_archive = archive_graph_snapshot(engine)
        with pytest.raises(RuntimeError, match="archived"):
            command.downgrade(config, PREVIOUS)
        assert current_schema_revision(engine) == REVISION
        assert archive_graph_snapshot(engine) == after_archive
        assert {name: rows for name, rows in before.items() if name != "user_brokers"} == {
            name: rows for name, rows in after_archive.items() if name != "user_brokers"
        }
    finally:
        engine.dispose()


def test_populated_archive_migration_preserves_history_and_refuses_lossy_downgrade(tmp_path: Path) -> None:
    check_populated_archive_migration(f"sqlite:///{tmp_path / 'broker-archive-migration.db'}")


def test_sqlite_downgrade_blocks_archive_between_empty_check_and_column_drop(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'concurrent-downgrade.db'}"
    config = migration_config(url)
    command.upgrade(config, "head")
    engine = create_database_engine(url)

    @event.listens_for(engine, "connect")
    def bound_writer_wait(connection, _record):
        connection.execute("PRAGMA busy_timeout=100")

    repository = UserBrokerRepository(create_session_factory(engine))
    created = repository.create(draft())
    archive_commits = []
    blocked_writers = []

    def archive_before_drop(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "DROP COLUMN archived_at" not in statement:
            return
        try:
            archive_commits.append(repository.archive(created.id, expected_environment="TEST"))
        except OperationalError as error:
            blocked_writers.append(str(error.orig).lower())

    event.listen(Engine, "before_cursor_execute", archive_before_drop)
    try:
        command.downgrade(config, PREVIOUS)
        assert archive_commits == []
        assert len(blocked_writers) == 1
        assert "locked" in blocked_writers[0]
        assert current_schema_revision(engine) == PREVIOUS
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT state FROM user_brokers WHERE id = :id"), {"id": created.id}) == "ACTIVE"
            )
    finally:
        event.remove(Engine, "before_cursor_execute", archive_before_drop)
        engine.dispose()
