import pytest
from sqlalchemy import create_engine

from tests.migrations.test_strict_state_invariants import (
    LEGACY_HOLES,
    assert_ddl_failure_is_atomic,
    assert_guard_is_atomic,
    assert_migration_roundtrip,
    migration_config,
)


@pytest.mark.postgresql
def test_postgresql_second_table_ddl_failure_rolls_back_first_constraint(isolated_postgresql_database_url):
    engine = create_engine(isolated_postgresql_database_url)
    try:
        assert_ddl_failure_is_atomic(migration_config(isolated_postgresql_database_url), engine)
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_migration_up_down_up_preserves_financial_and_admission_graph(isolated_postgresql_database_url):
    engine = create_engine(isolated_postgresql_database_url)
    try:
        assert_migration_roundtrip(migration_config(isolated_postgresql_database_url), engine)
    finally:
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize("legacy", LEGACY_HOLES)
def test_postgresql_migration_guard_rolls_back_atomically(isolated_postgresql_database_url, legacy):
    engine = create_engine(isolated_postgresql_database_url)
    try:
        assert_guard_is_atomic(migration_config(isolated_postgresql_database_url), engine, legacy)
    finally:
        engine.dispose()
