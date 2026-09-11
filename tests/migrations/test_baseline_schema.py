from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.schema_revision import current_schema_revision


def _upgrade(database_url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def test_empty_database_upgrades_to_head_and_is_idempotent(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    _upgrade(database_url)
    _upgrade(database_url)

    engine = create_engine(database_url)
    try:
        assert current_schema_revision(engine) == "0002_portfolio_snapshot_runs"
        assert set(inspect(engine).get_table_names()) == {*Base.metadata.tables, "alembic_version"}
    finally:
        engine.dispose()


def test_migrated_schema_matches_baseline_metadata(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'parity.db'}"
    _upgrade(database_url)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    try:
        for table_name, table in Base.metadata.tables.items():
            reflected = {column["name"]: column for column in inspector.get_columns(table_name)}
            assert set(reflected) == set(table.columns.keys())
            for column in table.columns:
                actual = reflected[column.name]
                assert actual["nullable"] is column.nullable
                assert actual["type"]._type_affinity is column.type._type_affinity

            expected_pk = tuple(column.name for column in table.primary_key.columns)
            assert tuple(inspector.get_pk_constraint(table_name)["constrained_columns"]) == expected_pk

            expected_unique = {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
            }
            actual_unique = {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}
            assert actual_unique == expected_unique

            expected_checks = {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
            }
            actual_checks = {constraint["name"] for constraint in inspector.get_check_constraints(table_name)}
            assert actual_checks == expected_checks

            expected_indexes = {index.name for index in table.indexes}
            actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            assert actual_indexes == expected_indexes
    finally:
        engine.dispose()
