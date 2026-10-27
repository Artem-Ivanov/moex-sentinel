import pytest
from sqlalchemy import text

from moex_sentinel.storage.database import create_database_engine
from tests.migrations.test_portfolio_run_index import assert_run_index_roundtrip, migration_config


@pytest.mark.postgresql
def test_postgresql_run_index_roundtrip_preserves_financial_graph_and_latest_ties(isolated_postgresql_database_url):
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        assert_run_index_roundtrip(migration_config(isolated_postgresql_database_url), engine)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT am.amname FROM pg_class idx JOIN pg_am am ON am.oid = idx.relam "
                        "WHERE idx.oid = to_regclass('ix_portfolio_snapshots_run_id')"
                    )
                )
                == "btree"
            )
    finally:
        engine.dispose()
