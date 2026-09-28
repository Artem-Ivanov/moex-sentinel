"""Short data sessions and the separate advisory-lock connection for collection."""

from contextlib import AbstractContextManager
from datetime import datetime
from sqlite3 import Connection as SQLiteConnection
from typing import cast

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.storage.repositories.portfolio_snapshots import (
    PortfolioSnapshotRepository,
    portfolio_snapshot_run_lock,
)


class PortfolioSnapshotCollectionStore:
    """Own collection persistence without holding data sessions across broker awaits."""

    def __init__(self, factory: sessionmaker[Session], engine: Engine) -> None:
        self._factory = factory
        self._engine = engine

    def acquire_run_lock(self, bucket_start: datetime) -> AbstractContextManager[bool]:
        """Hold the existing global collection lock on its own connection until context exit."""
        return portfolio_snapshot_run_lock(self._engine, bucket_start)

    def run_state(
        self, bucket_start: datetime
    ) -> tuple[PortfolioSnapshotRunValue | None, PortfolioSnapshotRunValue | None]:
        """Read the requested bucket and latest run in one short session."""
        with self._factory() as session:
            repository = PortfolioSnapshotRepository(session)
            return repository.run_for_bucket(bucket_start), repository.latest_run()

    def latest(self, user_broker_id: str, account_id: str, currency: str) -> PortfolioSnapshotValue | None:
        """Read one account baseline and release its session before further broker I/O."""
        with self._factory() as session:
            return PortfolioSnapshotRepository(session).latest(user_broker_id, account_id, currency)

    def save(
        self, run: PortfolioSnapshotRunValue, snapshots: tuple[PortfolioSnapshotValue, ...]
    ) -> PortfolioSnapshotRunValue:
        """Commit the complete run atomically and return the canonical winner of a bucket conflict."""
        with self._factory() as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                driver = cast(SQLiteConnection, connection.connection.driver_connection)
                # sqlite3's default transaction mode does not BEGIN for SELECT or SAVEPOINT: releasing
                # the repository savepoint must not commit the run before snapshots.
                if not driver.in_transaction:
                    connection.exec_driver_sql("BEGIN")
            persisted_run = PortfolioSnapshotRepository(session).append_run_with_snapshots(run, snapshots)
            session.commit()
            return persisted_run
