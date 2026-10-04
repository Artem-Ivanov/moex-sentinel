"""The identical scoped-lineage contract on migrated disposable PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from tests.storage.test_core_fact_lineage_reads import TestCoreFactLineageReads as _Contract
from tests.storage.test_core_fact_lineage_reads import lineage_factory as _shared_factory

lineage_factory = _shared_factory


@pytest.fixture
def lineage_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgresql
class TestPostgresqlCoreFactLineageReads(_Contract):
    pass
