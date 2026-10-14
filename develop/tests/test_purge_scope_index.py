"""Scoped maintenance index, verified only against an isolated PostgreSQL schema."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text

from develop.scripts.purge_scope_index import INDEX, manage_index
from develop.tests.test_purge_test_data import contents, seed
from moex_sentinel.storage.models import Base

pytest_plugins = ("tests.integration.postgresql.conftest",)


@pytest.fixture
def database(isolated_postgresql_database_url):
    engine = create_engine(isolated_postgresql_database_url)
    seed(engine)
    yield engine
    engine.dispose()


def test_default_read_only_and_concurrent_lifecycle_preserve_data_and_other_indexes(database):
    before = contents(database)
    with database.connect() as connection:
        indexes = inspect(connection).get_indexes("trade_audit_events")
    assert manage_index(database)["changed"] is False
    assert contents(database) == before
    assert manage_index(database, apply=True)["changed"] is True
    assert manage_index(database, apply=True)["changed"] is False
    assert contents(database) == before
    assert manage_index(database, drop=True)["changed"] is False
    assert manage_index(database, apply=True, drop=True)["changed"] is True
    assert manage_index(database, apply=True, drop=True)["changed"] is False
    assert contents(database) == before
    with database.connect() as connection:
        assert inspect(connection).get_indexes("trade_audit_events") == indexes


def test_name_conflict_refuses_create_and_drop(database):
    with database.begin() as connection:
        connection.exec_driver_sql(f"CREATE INDEX {INDEX} ON trade_audit_events (occurred_at)")
    before = contents(database)
    for drop in (False, True):
        with pytest.raises(ValueError, match="INDEX_NAME_CONFLICT"):
            manage_index(database, apply=True, drop=drop)
    assert contents(database) == before
    with database.connect() as connection:
        index = next(i for i in inspect(connection).get_indexes("trade_audit_events") if i["name"] == INDEX)
        assert index["column_names"] == ["occurred_at"]


def test_existing_foreign_key_scope_lookup_uses_small_index(database, tmp_path):
    audit = Base.metadata.tables["trade_audit_events"]
    with database.begin() as connection:
        row = dict(connection.execute(select(audit).where(audit.c.user_broker_id == "PROD")).mappings().one())
        connection.execute(audit.insert(), [dict(row, event_id=str(uuid4())) for _ in range(6000)])
        connection.execute(audit.delete().where(audit.c.user_broker_id == "TEST"))
        connection.exec_driver_sql("ANALYZE trade_audit_events")
    before_contents = contents(database)
    query = "SELECT 1 FROM trade_audit_events WHERE user_broker_id='TEST' AND execution_id='TEST-trade_executions'"

    def measure():
        with database.connect() as connection:
            assert not connection.execute(text(query)).all()
            return connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query).scalar_one()[0]

    before = measure()
    manage_index(database, apply=True)
    try:
        after = measure()
        assert before["Plan"]["Node Type"] == "Seq Scan"
        assert after["Plan"]["Node Type"] == "Index Scan"
        assert after["Plan"]["Shared Hit Blocks"] < before["Plan"]["Shared Hit Blocks"]
        assert contents(database) == before_contents
        evidence = {"rows": 6001, "before": before, "after": after}
        (tmp_path / "scope-index-benefit.json").write_text(json.dumps(evidence))
        print(  # noqa: T201 - Recorded benchmark evidence.
            json.dumps(
                {
                    "scope_index_benefit": {
                        "before_blocks": before["Plan"]["Shared Hit Blocks"],
                        "after_blocks": after["Plan"]["Shared Hit Blocks"],
                        "before_ms": before["Execution Time"],
                        "after_ms": after["Execution Time"],
                    }
                }
            )
        )
    finally:
        manage_index(database, apply=True, drop=True)
