"""Column collations must not broaden the serialized state alphabet."""

import sqlite3

import pytest

from tests.trading_automaton.storage.test_worker_state_migration import snapshot
from trading_automaton.storage import database
from trading_automaton.storage.state_schema import STATE_CHECKS, state_check_sql


@pytest.mark.parametrize(("collation", "invalid"), [("NOCASE", "filled"), ("RTRIM", "FILLED ")])
def test_collated_invalid_history_refuses_upgrade_atomically(tmp_path, collation, invalid):
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE broker_intents (id TEXT PRIMARY KEY, state TEXT COLLATE {collation})")
        db.execute("INSERT INTO broker_intents VALUES (?, ?)", ("old", invalid))
    before = snapshot(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    with pytest.raises(ValueError, match="Invalid historical Worker state"):
        database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path) == before


@pytest.mark.parametrize("collation", ["NOCASE", "RTRIM"])
def test_collated_states_stay_strict_across_upgrade_rollback(tmp_path, collation):
    path = tmp_path / "collated.db"
    with sqlite3.connect(path) as db:
        for table, (column, _, values) in STATE_CHECKS.items():
            db.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, {column} TEXT COLLATE {collation})")
            db.execute(f"INSERT INTO {table} VALUES (?, ?)", ("old", values[0]))  # noqa: S608
    engine = database.create_worker_engine(f"sqlite:///{path}")
    for _ in range(2):
        database.initialize_worker_schema(engine)
        with sqlite3.connect(path) as db:
            for table, (column, constraint, values) in STATE_CHECKS.items():
                ddl = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
                assert f"COLLATE {collation}" in ddl
                assert f"CONSTRAINT {constraint} CHECK ({state_check_sql(table)})" in ddl
                for invalid in (values[0].lower(), values[0] + " "):
                    with pytest.raises(sqlite3.IntegrityError):
                        db.execute(f"INSERT INTO {table} (id, {column}) VALUES (?, ?)", ("new", invalid))  # noqa: S608
                    with pytest.raises(sqlite3.IntegrityError):
                        db.execute(f"UPDATE {table} SET {column}=?", (invalid,))  # noqa: S608
        before = snapshot(path)[1]
        database.downgrade_worker_states(engine)
        assert snapshot(path)[1] == before
        with sqlite3.connect(path) as db:
            for table, (_, constraint, _) in STATE_CHECKS.items():
                ddl = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
                assert f"COLLATE {collation}" in ddl
                assert constraint not in ddl
    engine.dispose()


def test_fresh_metadata_uses_the_exact_same_binary_state_checks(tmp_path):
    engine = database.create_worker_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    database.initialize_worker_schema(engine)
    with engine.connect() as connection:
        for table, (column, constraint, _) in STATE_CHECKS.items():
            ddl = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).scalar_one()
            assert f"CONSTRAINT {constraint} CHECK ({state_check_sql(table)})" in ddl
            assert f"{column} COLLATE BINARY IN (" in ddl
    database.downgrade_worker_states(engine)
    database.initialize_worker_schema(engine)
    engine.dispose()
