"""PostgreSQL concurrency contract for portfolio snapshot runs."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.repositories.portfolio_snapshots import portfolio_snapshot_run_lock

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


@pytest.mark.postgresql
def test_portfolio_snapshot_advisory_lock_is_exclusive_and_released(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        with portfolio_snapshot_run_lock(engine, NOW) as first:
            assert first is True
            with portfolio_snapshot_run_lock(engine, NOW + timedelta(minutes=1)) as competing:
                assert competing is False

        with portfolio_snapshot_run_lock(engine, NOW) as reacquired:
            assert reacquired is True
    finally:
        engine.dispose()
