"""Worker SQLite connection policy."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Column, MetaData, Table, inspect, select

from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base, UTCDateTime


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
