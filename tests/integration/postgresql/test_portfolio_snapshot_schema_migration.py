"""Forward migration contract for the deployed portfolio snapshot schema."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, inspect, schema, text
from sqlalchemy.engine import URL

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.schema_revision import current_schema_revision


@pytest.fixture
def unmigrated_postgresql_database_url(postgresql_database_url: URL) -> Iterator[URL]:
    schema_name = f"snapshot_migration_{uuid4().hex}"
    admin_engine = create_database_engine(postgresql_database_url)
    with admin_engine.begin() as connection:
        connection.execute(schema.CreateSchema(schema_name))
    isolated_url = admin_engine.url.update_query_dict({"options": f"-csearch_path={schema_name}"})
    try:
        yield isolated_url
    finally:
        with admin_engine.begin() as connection:
            connection.execute(schema.DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


def _migration_config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    rendered_url = database_url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered_url)
    return config


def _wait_for_access_exclusive_lock(engine, table_name: str) -> None:
    deadline = monotonic() + 5
    query = text(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_locks
            WHERE relation = to_regclass(:table_name)
              AND mode = 'AccessExclusiveLock'
              AND granted = false
        )
        """
    )
    while monotonic() < deadline:
        with engine.connect() as connection:
            if connection.scalar(query, {"table_name": table_name}):
                return
        sleep(0.01)
    raise AssertionError(f"migration did not wait for {table_name} access-exclusive lock")


def _run_migration_in_thread(database_url: URL, revision: str) -> tuple[Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            command.upgrade(_migration_config(database_url), revision)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=migrate)
    thread.start()
    return thread, errors


@pytest.mark.postgresql
def test_upgrade_replaces_deployed_empty_snapshot_schema(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)
    command.upgrade(config, "0001_baseline")
    engine = create_database_engine(unmigrated_postgresql_database_url)
    try:
        baseline_inspector = inspect(engine)
        assert baseline_inspector.has_table("portfolio_snapshot_runs") is False
        assert {column["name"] for column in baseline_inspector.get_columns("portfolio_snapshots")} == {
            "id",
            "user_broker_id",
            "total_value",
            "free_cash",
            "realized_pnl",
            "unrealized_pnl",
            "net_pnl",
            "currency",
            "captured_at",
            "created_at",
        }

        command.upgrade(config, "head")
        head_inspector = inspect(engine)

        assert current_schema_revision(engine) == "0002_portfolio_snapshot_runs"
        assert head_inspector.has_table("portfolio_snapshot_runs") is True
        assert {column["name"] for column in head_inspector.get_columns("portfolio_snapshots")} == {
            "id",
            "run_id",
            "user_broker_id",
            "account_id",
            "total_value",
            "free_cash",
            "cumulative_pnl",
            "currency",
            "captured_at",
            "bucket_start",
            "created_at",
        }
        assert {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in head_inspector.get_unique_constraints("portfolio_snapshots")
        } == {
            "uq_portfolio_snapshots_account_currency_bucket": (
                "user_broker_id",
                "account_id",
                "currency",
                "bucket_start",
            )
        }
        assert {constraint["name"] for constraint in head_inspector.get_check_constraints("portfolio_snapshots")} == {
            "ck_portfolio_snapshots_non_negative_free_cash",
            "ck_portfolio_snapshots_non_negative_total_value",
        }
        assert {
            index["name"]: tuple(index["column_names"])
            for index in head_inspector.get_indexes("portfolio_snapshots")
            if index.get("duplicates_constraint") is None
        } == {
            "ix_portfolio_snapshots_account_currency_captured": (
                "user_broker_id",
                "account_id",
                "currency",
                "captured_at",
            ),
            "ix_portfolio_snapshots_captured_at": ("captured_at",),
        }
        assert {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in head_inspector.get_unique_constraints("portfolio_snapshot_runs")
        } == {"uq_portfolio_snapshot_runs_bucket": ("bucket_start",)}
        assert {
            index["name"]: tuple(index["column_names"])
            for index in head_inspector.get_indexes("portfolio_snapshot_runs")
            if index.get("duplicates_constraint") is None
        } == {"ix_portfolio_snapshot_runs_captured_at": ("captured_at",)}
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_upgrade_rejects_non_empty_deployed_snapshot_schema(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)
    command.upgrade(config, "0001_baseline")
    engine = create_database_engine(unmigrated_postgresql_database_url)
    metadata = MetaData()
    user_brokers = Table("user_brokers", metadata, autoload_with=engine)
    snapshots = Table("portfolio_snapshots", metadata, autoload_with=engine)
    moment = datetime(2026, 8, 15, 12, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                user_brokers.insert().values(
                    api_slug="synthetic-broker",
                    display_name="Synthetic broker",
                    environment="TEST",
                    fqdn="synthetic-broker.test",
                    settings={},
                    external_account_id=None,
                    state="DRAFT",
                    id="00000000-0000-0000-0000-000000000001",
                    created_at=moment,
                    updated_at=moment,
                )
            )
            connection.execute(
                snapshots.insert().values(
                    user_broker_id="00000000-0000-0000-0000-000000000001",
                    total_value=Decimal("100.00"),
                    free_cash=Decimal("40.00"),
                    realized_pnl=Decimal("2.00"),
                    unrealized_pnl=Decimal("3.00"),
                    net_pnl=Decimal("5.00"),
                    currency="RUB",
                    captured_at=moment,
                    created_at=moment,
                    id="00000000-0000-0000-0000-000000000002",
                )
            )

        with pytest.raises(RuntimeError, match="contains 1 row"):
            command.upgrade(config, "head")

        assert current_schema_revision(engine) == "0001_baseline"
        assert inspect(engine).has_table("portfolio_snapshot_runs") is False
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_empty_snapshot_schema_survives_downgrade_upgrade_round_trip(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0001_baseline")
    command.upgrade(config, "head")

    engine = create_database_engine(unmigrated_postgresql_database_url)
    try:
        assert current_schema_revision(engine) == "0002_portfolio_snapshot_runs"
        inspector = inspect(engine)
        assert inspector.has_table("portfolio_snapshot_runs") is True
        assert "cumulative_pnl" in {column["name"] for column in inspector.get_columns("portfolio_snapshots")}
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_upgrade_locks_snapshot_table_before_checking_that_it_is_empty(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)
    command.upgrade(config, "0001_baseline")
    engine = create_database_engine(unmigrated_postgresql_database_url)
    metadata = MetaData()
    user_brokers = Table("user_brokers", metadata, autoload_with=engine)
    snapshots = Table("portfolio_snapshots", metadata, autoload_with=engine)
    moment = datetime(2026, 8, 15, 12, tzinfo=UTC)
    try:
        blocker = engine.connect()
        transaction = blocker.begin()
        blocker.execute(text("LOCK TABLE portfolio_snapshots IN ACCESS SHARE MODE"))
        thread, errors = _run_migration_in_thread(unmigrated_postgresql_database_url, "head")
        _wait_for_access_exclusive_lock(engine, "portfolio_snapshots")
        blocker.execute(
            user_brokers.insert().values(
                api_slug="synthetic-broker",
                display_name="Synthetic broker",
                environment="TEST",
                fqdn="synthetic-broker.test",
                settings={},
                external_account_id=None,
                state="DRAFT",
                id="00000000-0000-0000-0000-000000000011",
                created_at=moment,
                updated_at=moment,
            )
        )
        blocker.execute(
            snapshots.insert().values(
                user_broker_id="00000000-0000-0000-0000-000000000011",
                total_value=Decimal("100.00"),
                free_cash=Decimal("40.00"),
                realized_pnl=Decimal("2.00"),
                unrealized_pnl=Decimal("3.00"),
                net_pnl=Decimal("5.00"),
                currency="RUB",
                captured_at=moment,
                created_at=moment,
                id="00000000-0000-0000-0000-000000000012",
            )
        )
        transaction.commit()
        blocker.close()
        thread.join(timeout=10)

        assert thread.is_alive() is False
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert current_schema_revision(engine) == "0001_baseline"
        assert inspect(engine).has_table("portfolio_snapshots") is True
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_downgrade_rejects_non_empty_run_schema(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(unmigrated_postgresql_database_url)
    metadata = MetaData()
    runs = Table("portfolio_snapshot_runs", metadata, autoload_with=engine)
    moment = datetime(2026, 8, 15, 12, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                runs.insert().values(
                    captured_at=moment,
                    bucket_start=moment,
                    safe_errors=[],
                    created_at=moment,
                    id="00000000-0000-0000-0000-000000000003",
                )
            )

        with pytest.raises(RuntimeError, match="contains 1 row"):
            command.downgrade(config, "0001_baseline")

        assert current_schema_revision(engine) == "0002_portfolio_snapshot_runs"
        assert inspect(engine).has_table("portfolio_snapshot_runs") is True
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_downgrade_locks_both_snapshot_tables_before_empty_checks(
    unmigrated_postgresql_database_url: URL,
) -> None:
    config = _migration_config(unmigrated_postgresql_database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(unmigrated_postgresql_database_url)
    metadata = MetaData()
    runs = Table("portfolio_snapshot_runs", metadata, autoload_with=engine)
    moment = datetime(2026, 8, 15, 12, tzinfo=UTC)
    try:
        blocker = engine.connect()
        transaction = blocker.begin()
        blocker.execute(text("LOCK TABLE portfolio_snapshots IN ACCESS SHARE MODE"))
        errors: list[BaseException] = []

        def downgrade() -> None:
            try:
                command.downgrade(_migration_config(unmigrated_postgresql_database_url), "0001_baseline")
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=downgrade)
        thread.start()
        _wait_for_access_exclusive_lock(engine, "portfolio_snapshots")
        blocker.execute(
            runs.insert().values(
                captured_at=moment,
                bucket_start=moment,
                safe_errors=[],
                created_at=moment,
                id="00000000-0000-0000-0000-000000000013",
            )
        )
        transaction.commit()
        blocker.close()
        thread.join(timeout=10)

        assert thread.is_alive() is False
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert current_schema_revision(engine) == "0002_portfolio_snapshot_runs"
        assert inspect(engine).has_table("portfolio_snapshot_runs") is True
    finally:
        engine.dispose()
