"""PostgreSQL concurrency contract for portfolio snapshot runs."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import URL

from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.portfolio_snapshot_collection import PortfolioSnapshotCollectionStore
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


@pytest.mark.postgresql
def test_collection_store_releases_run_lock_after_failure(isolated_postgresql_database_url: URL) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    store = PortfolioSnapshotCollectionStore(create_session_factory(engine), engine)

    def fail_during_collection() -> None:
        with store.acquire_run_lock(NOW) as acquired:
            assert acquired is True
            raise RuntimeError("synthetic collection failure")

    try:
        with pytest.raises(RuntimeError, match="synthetic collection failure"):
            fail_during_collection()

        with store.acquire_run_lock(NOW + timedelta(minutes=1)) as reacquired:
            assert reacquired is True
    finally:
        engine.dispose()
