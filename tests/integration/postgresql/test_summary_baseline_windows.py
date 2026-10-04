"""Summary windows contract on migrated disposable PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from tests.storage.test_portfolio_snapshot_repository import TestSummaryBaselineWindows as _Contract
from tests.storage.test_portfolio_snapshot_repository import summary_session as _shared_session

summary_session = _shared_session


@pytest.fixture
def summary_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgresql
class TestPostgresqlSummaryBaselineWindows(_Contract):
    pass
