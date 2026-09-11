from sqlalchemy import create_engine, text

from moex_sentinel.storage.schema_revision import (
    current_schema_revision,
    expected_schema_revision,
    schema_is_compatible,
)


def test_database_without_alembic_table_has_no_current_revision() -> None:
    engine = create_engine("sqlite:///:memory:")

    assert current_schema_revision(engine) is None


def test_reads_current_database_revision() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('revision-1')"))

    assert current_schema_revision(engine) == "revision-1"


def test_schema_is_compatible_only_for_expected_revision() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('revision-1')"))

    assert schema_is_compatible(engine, "revision-1") is True
    assert schema_is_compatible(engine, "revision-2") is False


def test_expected_revision_is_current_alembic_head() -> None:
    assert expected_schema_revision() == "0002_portfolio_snapshot_runs"
