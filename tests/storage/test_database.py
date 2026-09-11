from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Column, MetaData, Table, select, text
from sqlalchemy.exc import StatementError

from moex_sentinel.config import Settings
from moex_sentinel.storage.database import (
    create_database_engine,
    create_session_factory,
    session_scope,
)
from moex_sentinel.storage.types import UTCDateTime


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'sentinel.db'}"


def test_file_database_enables_required_sqlite_pragmas(database_url: str) -> None:
    engine = create_database_engine(database_url)

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000


def test_session_scope_commits_successful_transaction(database_url: str) -> None:
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE records (value INTEGER NOT NULL)"))
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        session.execute(text("INSERT INTO records (value) VALUES (1)"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM records")).scalar_one() == 1


def test_session_scope_rolls_back_failed_transaction(database_url: str) -> None:
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE records (value INTEGER NOT NULL)"))
    factory = create_session_factory(engine)

    def write_then_fail() -> None:
        with session_scope(factory) as session:
            session.execute(text("INSERT INTO records (value) VALUES (1)"))
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        write_then_fail()

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM records")).scalar_one() == 0


def test_utc_datetime_round_trips_as_aware_utc(database_url: str) -> None:
    engine = create_database_engine(database_url)
    metadata = MetaData()
    moments = Table("moments", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)
    original = datetime(2026, 8, 4, 19, 0, tzinfo=UTC)

    with engine.begin() as connection:
        connection.execute(moments.insert().values(value=original))
        restored = connection.execute(select(moments.c.value)).scalar_one()

    assert restored == original
    assert restored.tzinfo is UTC


def test_utc_datetime_floors_persisted_values_to_milliseconds(database_url: str) -> None:
    engine = create_database_engine(database_url)
    metadata = MetaData()
    moments = Table("moments", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(moments.insert().values(value=datetime(2026, 8, 13, 10, 0, 0, 123001, tzinfo=UTC)))
        restored = connection.execute(select(moments.c.value)).scalar_one()

    assert restored == datetime(2026, 8, 13, 10, 0, 0, 123000, tzinfo=UTC)


def test_utc_datetime_rejects_naive_value(database_url: str) -> None:
    engine = create_database_engine(database_url)
    metadata = MetaData()
    moments = Table("moments", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)

    with pytest.raises(StatementError, match="timezone-aware"), engine.begin() as connection:
        connection.execute(moments.insert().values(value=datetime(2026, 8, 4, 19, 0)))


def test_settings_accepts_postgresql_and_pool_configuration() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://db-host/sentinel",
        database_pool_size=7,
        database_max_overflow=3,
        database_pool_timeout_seconds=2.5,
    )

    assert settings.database_pool_size == 7
    assert settings.database_max_overflow == 3
    assert settings.database_pool_timeout_seconds == 2.5


def test_settings_rejects_unsupported_database_dialect() -> None:
    with pytest.raises(ValueError, match="SQLite or PostgreSQL"):
        Settings(database_url="mysql://db-host/sentinel")


@pytest.mark.parametrize(
    ("setting_name", "invalid_value"),
    [
        ("database_pool_size", 0),
        ("database_max_overflow", -1),
        ("database_pool_timeout_seconds", 0),
    ],
)
def test_settings_rejects_invalid_pool_configuration(setting_name: str, invalid_value: int) -> None:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://db-host/sentinel",
        setting_name: invalid_value,
    }

    with pytest.raises(ValueError, match="database pool"):
        Settings(**values)  # type: ignore[arg-type]
