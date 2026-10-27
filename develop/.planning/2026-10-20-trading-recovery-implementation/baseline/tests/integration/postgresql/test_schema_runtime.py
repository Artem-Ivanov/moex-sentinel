from datetime import UTC, datetime

import pytest
from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.schema_revision import (
    current_schema_revision,
    expected_schema_revision,
    schema_is_compatible,
)
from moex_sentinel.storage.types import UTCDateTime


@pytest.mark.postgresql
def test_migrated_postgresql_schema_and_utc_round_trip(isolated_postgresql_database_url: URL) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    metadata = MetaData()
    moments = Table("task3_timestamp_acceptance", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)
    original = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(moments.delete())
            connection.execute(moments.insert().values(value=original))
            restored = connection.execute(select(moments.c.value)).scalar_one()

        expected = expected_schema_revision()
        assert current_schema_revision(engine) == expected
        assert schema_is_compatible(engine, expected) is True
        assert restored == original
        assert restored.tzinfo is UTC
    finally:
        metadata.drop_all(engine)
        engine.dispose()
