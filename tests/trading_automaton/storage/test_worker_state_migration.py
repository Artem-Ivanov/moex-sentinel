"""State migration round-trips actual SQLite DDL and rows under a write lock."""

# SQL identifiers below come exclusively from the isolated test schema.
# ruff: noqa: S608
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import NOW, baseline_command, filled_buy
from trading_automaton.storage import database
from trading_automaton.storage.repository import LocalAutomationRepository


def legacy(path):
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        for table, column, value in (
            ("cached_automations", "state", "IN_WORK"),
            ("broker_intents", "state", "FAILED"),
            ("fact_outbox", "delivery_state", "PENDING"),
        ):
            db.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, {column} TEXT, extra BLOB, amount NUMERIC)")
            db.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", ("key", value, b"\x00\xff", "123.123456789"))
        db.execute("CREATE TABLE audit (value TEXT)")
        db.execute("CREATE INDEX extra_index ON broker_intents(extra) WHERE amount > 0")
        db.execute(
            "CREATE TRIGGER own_trigger AFTER UPDATE ON broker_intents BEGIN INSERT INTO audit VALUES (new.state); END"
        )


def snapshot(path):
    with sqlite3.connect(path) as db:
        schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name").fetchall()
        rows = {
            name: db.execute(f'SELECT * FROM "{name}"').fetchall() for kind, name, _, _ in schema if kind == "table"
        }
        return schema, rows, db.execute("PRAGMA user_version").fetchone()[0]


def test_legacy_upgrade_checks_each_state_column_and_roundtrips_extensions(tmp_path):
    path = tmp_path / "worker.db"
    legacy(path)
    original = snapshot(path)[1]
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.initialize_worker_schema(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        for table, column in [
            ("cached_automations", "state"),
            ("broker_intents", "state"),
            ("fact_outbox", "delivery_state"),
        ]:
            for invalid in ("TYPO", None):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(f"UPDATE {table} SET {column} = ?", (invalid,))
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(f"INSERT INTO {table} (id, {column}) VALUES (?, ?)", ("invalid", invalid))
            assert db.execute(f"SELECT id, {column}, extra, amount FROM {table}").fetchall() == original[table]
        db.execute('ALTER TABLE broker_intents ADD COLUMN later TEXT DEFAULT "saved"')
        db.execute("CREATE INDEX later_index ON broker_intents(later)")
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.downgrade_worker_states(engine)
    engine.dispose()
    assert snapshot(path)[2] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT later FROM broker_intents").fetchone() == ("saved",)
        db.execute("UPDATE broker_intents SET state = 'TYPO'")
        assert db.execute("SELECT value FROM audit").fetchall() == [("TYPO",)]
        db.execute("UPDATE broker_intents SET state = 'FAILED'")
        assert {r[1] for r in db.execute("PRAGMA index_list(broker_intents)")} >= {"later_index", "extra_index"}
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.initialize_worker_schema(engine)
    database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path)[2] == 1


@pytest.mark.parametrize(
    "problem", ["future", "invalid-third", "generated", "view", "foreign-key", "external-trigger", "autoincrement"]
)
def test_unsupported_legacy_is_unchanged(tmp_path, problem):
    path = tmp_path / "worker.db"
    legacy(path)
    with sqlite3.connect(path) as db:
        statements = {
            "future": "PRAGMA user_version=2",
            "invalid-third": "UPDATE fact_outbox SET delivery_state='TYPO'",
            "generated": "ALTER TABLE broker_intents ADD COLUMN generated TEXT AS (state)",
            "view": "CREATE VIEW external_view AS SELECT * FROM broker_intents",
            "foreign-key": "CREATE TABLE child (parent TEXT REFERENCES broker_intents(id))",
            "external-trigger": (
                "CREATE TRIGGER external_trigger AFTER INSERT ON audit BEGIN UPDATE broker_intents SET amount=0; END"
            ),
            "autoincrement": "CREATE TABLE _extra (id INTEGER PRIMARY KEY AUTOINCREMENT)",
        }
        db.execute(statements[problem])
        if problem == "autoincrement":
            db.execute("DROP TABLE broker_intents")
            db.execute("ALTER TABLE _extra RENAME TO broker_intents")
    before = snapshot(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    with pytest.raises(ValueError, match="Unsupported|Invalid"):
        database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path) == before


def test_second_rebuild_failure_is_atomic(tmp_path):
    path = tmp_path / "worker.db"
    legacy(path)
    before = snapshot(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "before_cursor_execute")
    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith('DROP TABLE "broker_intents"'):
            raise RuntimeError("injected second rebuild failure")

    with pytest.raises(RuntimeError, match="second rebuild"):
        database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path) == before


def test_full_worker_ledger_outbox_and_identity_survive_wal_roundtrip(tmp_path):

    path = tmp_path / "full.db"
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.initialize_worker_schema(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    command = baseline_command()
    repo.cache_command(command)
    repo.save_decision_batch((decision_item(command),), occurred_at=NOW)
    repo.finalize_execution(filled_buy())
    database.downgrade_worker_states(engine)
    engine.dispose()
    before = snapshot(path)[1]
    with sqlite3.connect(path) as db:
        identities = {table: db.execute(f'SELECT rowid FROM "{table}"').fetchall() for table in before}
    for action in (
        database.initialize_worker_schema,
        database.downgrade_worker_states,
        database.initialize_worker_schema,
    ):
        engine = database.create_worker_engine(f"sqlite:///{path}")
        action(engine)
        engine.dispose()
        assert snapshot(path)[1] == before
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert {table: db.execute(f'SELECT rowid FROM "{table}"').fetchall() for table in before} == identities


def test_preflight_holds_writer_lock_and_downgrade_failure_is_atomic(tmp_path):
    path = tmp_path / "locked.db"
    legacy(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    observed = []

    @event.listens_for(engine, "before_cursor_execute")
    def check_lock(_conn, _cursor, statement, _parameters, _context, _many):
        if statement == "PRAGMA user_version":
            with sqlite3.connect(path, timeout=0) as writer:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute("UPDATE fact_outbox SET delivery_state='FAILED'")
                observed.append(True)

    database.initialize_worker_schema(engine)
    assert observed == [True]
    before = snapshot(path)

    @event.listens_for(engine, "before_cursor_execute")
    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith('DROP TABLE "broker_intents"'):
            raise RuntimeError("downgrade injected failure")

    with pytest.raises(RuntimeError, match="injected"):
        database.downgrade_worker_states(engine)
    engine.dispose()
    assert snapshot(path) == before


def test_operator_rollback_uses_existing_file_and_never_initializes(tmp_path):

    path = tmp_path / "operator.db"
    script = "develop/scripts/rollback_worker_states.py"
    missing = subprocess.run([sys.executable, script, str(path)], check=False, capture_output=True, text=True)
    assert missing.returncode != 0
    assert not path.exists()
    legacy(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.initialize_worker_schema(engine)
    engine.dispose()
    before = snapshot(path)[1]
    result = subprocess.run([sys.executable, script, str(path)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert snapshot(path)[1] == before
    assert snapshot(path)[2] == 0


def test_shadowed_rowid_extension_preserves_internal_identity(tmp_path):
    path = tmp_path / "shadowed.db"
    legacy(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE broker_intents ADD COLUMN ROWID TEXT DEFAULT "extension"')
        db.execute("UPDATE broker_intents SET _rowid_ = 42")
    engine = database.create_worker_engine(f"sqlite:///{path}")
    database.initialize_worker_schema(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT _rowid_, ROWID FROM broker_intents").fetchone() == (42, "extension")


def test_reopen_valid_revision_does_not_rescan_historical_order_rows(tmp_path):
    engine = database.create_worker_engine(f"sqlite:///{tmp_path / 'reopen.db'}")
    database.initialize_worker_schema(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    database.initialize_worker_schema(engine)
    engine.dispose()
    assert not any("WHERE NOT (" in statement for statement in statements)


@pytest.mark.parametrize("version", [0, 1])
def test_ambiguous_named_constraint_is_refused_without_touching_rows(tmp_path, version):
    path = tmp_path / "ambiguous.db"
    legacy(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE fact_outbox")
        db.execute(
            "CREATE TABLE fact_outbox (id TEXT PRIMARY KEY, delivery_state TEXT, "
            "CONSTRAINT ck_worker_outbox_state CHECK (1))"
        )
        db.execute("INSERT INTO fact_outbox VALUES ('key', 'PENDING')")
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    with pytest.raises(ValueError, match="Ambiguous"):
        database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE CACHED_AUTOMATIONS (automation_id TEXT PRIMARY KEY, state TEXT)",
        'CREATE VIEW cached_automations AS SELECT 1 AS automation_id, "IN_WORK" AS state',
    ],
)
def test_ambiguous_state_table_identity_is_refused_atomically(tmp_path, ddl):
    path = tmp_path / "ambiguous-table.db"
    with sqlite3.connect(path) as db:
        db.execute(ddl)
    before = snapshot(path)
    engine = database.create_worker_engine(f"sqlite:///{path}")
    with pytest.raises(ValueError, match="Ambiguous"):
        database.initialize_worker_schema(engine)
    engine.dispose()
    assert snapshot(path) == before
