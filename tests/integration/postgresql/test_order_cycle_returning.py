"""The same aggregate replacement contract on migrated PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from tests.storage.test_order_cycle_returning import TestOrderCycleReturning as _Contract
from tests.storage.test_order_cycle_returning import replacement_factory as _shared_factory

replacement_factory = _shared_factory


@pytest.fixture
def replacement_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgresql
class TestPostgresqlOrderCycleReturning(_Contract):
    pass
