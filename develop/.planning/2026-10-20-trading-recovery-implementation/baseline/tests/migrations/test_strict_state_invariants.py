from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event, inspect, text

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.schema_revision import current_schema_revision
from tests.migrations.strict_state_helpers import (
    BOOTSTRAP,
    INVALID_RUNTIME,
    assert_strict_insert_behavior,
    insert_bootstrap,
    insert_runtime,
)
from tests.migrations.user_broker_archive_helpers import archive_graph_snapshot, seed_archive_graph

OLD_REVISION = "0005_order_quota_percent"
NEW_REVISION = "0006_strict_state_invariants"
LEGACY_HOLES = [
    {**BOOTSTRAP, "bootstrap_quantity_lots": None},
    {**BOOTSTRAP, "bootstrap_average_price": None},
    {**BOOTSTRAP, "bootstrap_invested_amount": None},
    *INVALID_RUNTIME[:3],
]


def migration_config(database_url) -> Config:
    config = Config("alembic.ini")
    if not isinstance(database_url, str):
        database_url = database_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def constraints(engine):
    return {
        table: inspect(engine).get_check_constraints(table)
        for table in ("trading_automations", "worker_runtime_instances")
    }


def seed_referenced_graph(engine):
    seed_archive_graph(engine)
    insert_runtime(engine, "OWNER", 1, BOOTSTRAP["bootstrap_observed_at"])
    with engine.begin() as connection:
        runtime_id = connection.scalar(text("SELECT runtime_instance_id FROM worker_runtime_instances"))
        connection.execute(
            text(
                "INSERT INTO trading_account_admissions "
                "(user_broker_id, revision, owner_runtime_instance_id, owner_generation, "
                "admission_status, recovery_only, created_at, updated_at) "
                "VALUES ('scope-1', 1, :runtime, 1, 'DISARMED', false, :now, :now)"
            ),
            {"runtime": runtime_id, "now": BOOTSTRAP["bootstrap_observed_at"].isoformat()},
        )


def assert_migration_roundtrip(config, engine):
    command.downgrade(config, OLD_REVISION)
    seed_referenced_graph(engine)
    before = archive_graph_snapshot(engine)
    old_checks = constraints(engine)
    command.upgrade(config, NEW_REVISION)
    assert current_schema_revision(engine) == NEW_REVISION
    assert archive_graph_snapshot(engine) == before
    assert_strict_insert_behavior(engine)
    after_insert = archive_graph_snapshot(engine)
    command.downgrade(config, OLD_REVISION)
    assert current_schema_revision(engine) == OLD_REVISION
    assert constraints(engine) == old_checks
    assert archive_graph_snapshot(engine) == after_insert
    command.upgrade(config, NEW_REVISION)
    assert archive_graph_snapshot(engine) == after_insert
    assert_strict_insert_behavior(engine)


def assert_guard_is_atomic(config, engine, legacy):
    command.downgrade(config, OLD_REVISION)
    seed_referenced_graph(engine)
    if isinstance(legacy, dict):
        insert_bootstrap(engine, legacy)
    else:
        insert_runtime(engine, *legacy)
    before = archive_graph_snapshot(engine)
    old_checks = constraints(engine)
    with pytest.raises(RuntimeError, match="Cannot enforce strict state invariants"):
        command.upgrade(config, NEW_REVISION)
    assert current_schema_revision(engine) == OLD_REVISION
    assert archive_graph_snapshot(engine) == before
    assert constraints(engine) == old_checks
    assert not any(name.startswith("_alembic_tmp") for name in inspect(engine).get_table_names())


def assert_ddl_failure_is_atomic(config, engine):
    command.downgrade(config, OLD_REVISION)
    seed_referenced_graph(engine)
    before = archive_graph_snapshot(engine)
    old_checks = constraints(engine)

    def fail_second_table(connection, cursor, statement, parameters, context, executemany):
        if "worker_runtime_instances" in statement and (
            statement.lstrip().startswith("CREATE TABLE _alembic_tmp_")
            or "ADD CONSTRAINT ck_worker_runtime_generation_status" in statement
        ):
            raise RuntimeError("injected second table DDL failure")

    event.listen(Engine, "before_cursor_execute", fail_second_table)
    try:
        with pytest.raises(RuntimeError, match="injected second table DDL failure"):
            command.upgrade(config, NEW_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_second_table)
    assert current_schema_revision(engine) == OLD_REVISION
    assert constraints(engine) == old_checks
    assert archive_graph_snapshot(engine) == before
    assert not any(name.startswith("_alembic_tmp") for name in inspect(engine).get_table_names())


def test_sqlite_failure_during_second_table_rebuild_rolls_back_first_rebuild(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'strict-ddl-failure.db'}"
    config = migration_config(url)
    command.upgrade(config, OLD_REVISION)
    engine = create_engine(url)
    try:
        assert_ddl_failure_is_atomic(config, engine)
    finally:
        engine.dispose()


def test_sqlite_migration_up_down_up_preserves_financial_and_admission_graph(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'strict-state.db'}"
    config = migration_config(url)
    command.upgrade(config, OLD_REVISION)
    engine = create_engine(url)
    try:
        assert_migration_roundtrip(config, engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("legacy", LEGACY_HOLES)
def test_sqlite_migration_refuses_legacy_holes_without_changing_rows_or_schema(tmp_path: Path, legacy):
    url = f"sqlite:///{tmp_path / 'strict-guard.db'}"
    config = migration_config(url)
    command.upgrade(config, OLD_REVISION)
    engine = create_engine(url)
    try:
        assert_guard_is_atomic(config, engine, legacy)
    finally:
        engine.dispose()
