"""Valid automation projection and read-budget contracts on PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from tests.storage.test_automation_read_model import TestAutomationReadModel as _Contract
from tests.storage.test_automation_read_model import read_model_factory as _shared_factory

read_model_factory = _shared_factory


@pytest.fixture
def read_model_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgresql
class TestPostgresqlAutomationReadModel(_Contract):
    pass
