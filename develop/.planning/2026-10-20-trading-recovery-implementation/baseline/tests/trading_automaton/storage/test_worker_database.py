"""Worker SQLite connection policy."""

import runpy
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Column, MetaData, Table, inspect, select

from trading_automaton.storage import database
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base, TradeDecisionModel, UTCDateTime


def test_worker_sqlite_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
    engine.dispose()


def test_worker_engine_rejects_postgresql_url() -> None:
    with pytest.raises(ValueError, match="private SQLite"):
        create_worker_engine("postgresql+psycopg://database/worker")


def test_worker_datetime_floors_persisted_values_to_milliseconds(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    metadata = MetaData()
    moments = Table("moments", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(moments.insert().values(value=datetime(2026, 8, 13, 10, 0, 0, 123001, tzinfo=UTC)))
        restored = connection.execute(select(moments.c.value)).scalar_one()

    assert restored == datetime(2026, 8, 13, 10, 0, 0, 123000, tzinfo=UTC)
    engine.dispose()


def test_clean_worker_schema_has_one_typed_fact_outbox(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker-baseline.db'}")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert "fact_outbox" in tables
    assert "fact_outbox_v2" not in tables
    assert "outbox_events" not in tables
    engine.dispose()


def test_worker_schema_upgrades_existing_automation_cache_without_losing_rows(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'old-worker.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE cached_automations (automation_id VARCHAR(36) PRIMARY KEY, state VARCHAR(16) NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO cached_automations (automation_id, state) VALUES ('automation-1', 'IN_WORK')"
            )

        database.initialize_worker_schema(engine)

        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(engine).get_columns("cached_automations")}
            row = connection.exec_driver_sql(
                "SELECT state, settings_revision, order_quota_percent FROM cached_automations "
                "WHERE automation_id = 'automation-1'"
            ).one()
        assert {"settings_revision", "order_quota_percent"} <= columns
        assert row == ("IN_WORK", 0, None)
    finally:
        engine.dispose()


def test_quota_rollback_refuses_saved_values_then_drops_only_new_columns(tmp_path) -> None:
    database_path = tmp_path / "rollback.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE cached_automations (automation_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "settings_revision INTEGER NOT NULL DEFAULT 0, order_quota_percent NUMERIC(28,9))"
        )
        connection.execute("INSERT INTO cached_automations VALUES ('automation-1', 'IN_WORK', 1, 12.5)")
    script = Path("develop/position-quota/rollback_worker_quota.py").resolve()

    refused = subprocess.run(
        [sys.executable, str(script), str(database_path)], check=False, capture_output=True, text=True
    )
    assert refused.returncode != 0
    assert "refusing to remove saved automation settings" in refused.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT settings_revision, order_quota_percent FROM cached_automations"
        ).fetchone() == (1, 12.5)
        connection.execute("UPDATE cached_automations SET order_quota_percent = NULL")

    rolled_back = subprocess.run(
        [sys.executable, str(script), str(database_path)], check=False, capture_output=True, text=True
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cached_automations)")}
        assert "settings_revision" not in columns
        assert "order_quota_percent" not in columns
        assert connection.execute("SELECT automation_id, state FROM cached_automations").fetchone() == (
            "automation-1",
            "IN_WORK",
        )


def test_quota_rollback_serializes_writers_before_checking_saved_settings(tmp_path) -> None:
    database_path = tmp_path / "rollback-lock.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE cached_automations (automation_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "settings_revision INTEGER NOT NULL DEFAULT 0, order_quota_percent NUMERIC(28,9))"
        )
        connection.execute("INSERT INTO cached_automations VALUES ('automation-1', 'IN_WORK', 1, NULL)")

    rollback = runpy.run_path("develop/position-quota/rollback_worker_quota.py")["rollback"]
    guard_reached = threading.Event()
    continue_rollback = threading.Event()
    failures = []

    def run_rollback() -> None:
        try:
            with sqlite3.connect(database_path) as connection:
                connection.set_trace_callback(
                    lambda statement: (
                        (
                            guard_reached.set(),
                            continue_rollback.wait(timeout=2),
                        )
                        if statement.startswith("SELECT 1 FROM cached_automations")
                        else None
                    )
                )
                rollback(connection)
        except Exception as error:  # pragma: no cover - asserted through worker result
            failures.append(error)

    worker = threading.Thread(target=run_rollback)
    worker.start()
    assert guard_reached.wait(timeout=2)
    with sqlite3.connect(database_path, timeout=0) as writer, pytest.raises(sqlite3.OperationalError, match="locked"):
        writer.execute("UPDATE cached_automations SET order_quota_percent = 12.5")
    continue_rollback.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert failures == []
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cached_automations)")}
        assert "settings_revision" not in columns
        assert "order_quota_percent" not in columns
        assert connection.execute("SELECT automation_id, state FROM cached_automations").fetchone() == (
            "automation-1",
            "IN_WORK",
        )


def test_quota_rollback_second_drop_failure_preserves_both_columns(tmp_path) -> None:
    database_path = tmp_path / "rollback-atomic.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE cached_automations (automation_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "settings_revision INTEGER NOT NULL DEFAULT 0, order_quota_percent NUMERIC(28,9))"
        )
        connection.execute("INSERT INTO cached_automations VALUES ('automation-1', 'IN_WORK', 1, NULL)")
        connection.execute("CREATE INDEX keep_settings_revision ON cached_automations(settings_revision)")

    script = Path("develop/position-quota/rollback_worker_quota.py").resolve()
    result = subprocess.run(
        [sys.executable, str(script), str(database_path)], check=False, capture_output=True, text=True
    )

    assert result.returncode != 0
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cached_automations)")}
        assert {"settings_revision", "order_quota_percent"} <= columns
        assert connection.execute(
            "SELECT automation_id, state, settings_revision, order_quota_percent FROM cached_automations"
        ).fetchone() == ("automation-1", "IN_WORK", 1, None)


def test_quota_rollback_does_not_create_missing_database(tmp_path) -> None:
    database_path = tmp_path / "missing.sqlite"
    script = Path("develop/position-quota/rollback_worker_quota.py").resolve()

    result = subprocess.run(
        [sys.executable, str(script), str(database_path)], check=False, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert not database_path.exists()


@pytest.mark.parametrize("existing", [pytest.param(False, id="fresh"), pytest.param(True, id="existing-with-rows")])
def test_worker_schema_adds_decision_lookup_index_without_changing_rows(tmp_path, existing) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'upgrade.sqlite'}")
    try:
        if existing:
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP INDEX IF EXISTS ix_trade_decisions_intent_id")
                values = {
                    "id": "decision-1",
                    "automation_id": "automation-1",
                    "broker_id": "broker-1",
                    "instrument_id": "instrument-1",
                    "occurred_at": datetime(2026, 9, 16, tzinfo=UTC),
                    "quantity_lots": 1,
                    "lot_size": 10,
                    "average_price": Decimal("100.123456789"),
                    "current_price": Decimal("101"),
                    "best_bid": Decimal("101"),
                    "best_ask": Decimal("102"),
                    "invested_amount": Decimal("1001.234567890"),
                    "estimated_commission": Decimal("0.125"),
                    "decision": "BUY_MORE",
                    "reason_code": "TEST",
                    "decision_quantity_lots": 1,
                    "strategy_snapshot": {"source": "synthetic"},
                    "intent_id": "intent-1",
                }
                connection.execute(TradeDecisionModel.__table__.insert().values(**values))
                before = connection.execute(select(TradeDecisionModel.__table__)).mappings().all()
        else:
            before = []

        database.initialize_worker_schema(engine)
        database.initialize_worker_schema(engine)

        index = next(
            item
            for item in inspect(engine).get_indexes("trade_decisions")
            if item["name"] == "ix_trade_decisions_intent_id"
        )
        assert index["column_names"] == ["intent_id"]
        assert not index["unique"]
        with engine.connect() as connection:
            assert connection.execute(select(TradeDecisionModel.__table__)).mappings().all() == before
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM trade_decisions WHERE intent_id = ?", ("intent-1",)
            ).all()
        assert any("SEARCH" in row[3] and "ix_trade_decisions_intent_id" in row[3] for row in plan)
    finally:
        engine.dispose()
