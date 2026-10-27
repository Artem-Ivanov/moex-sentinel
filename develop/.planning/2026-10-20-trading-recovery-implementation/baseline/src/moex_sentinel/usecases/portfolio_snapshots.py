"""One locked portfolio collection run with an atomic final persistence step."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import (
    PortfolioSnapshotRunResult,
    PortfolioSnapshotRunValue,
    PortfolioSnapshotValue,
)
from sentinel_contracts.time import floor_utc_millisecond


class PortfolioCollectionPort(Protocol):
    async def collect(
        self, run_id: str, captured_at: datetime, bucket_start: datetime
    ) -> tuple[tuple[PortfolioSnapshotValue, ...], tuple[BrokerReadError, ...]]: ...


class PortfolioCollectionStorePort(Protocol):
    def acquire_run_lock(self, bucket_start: datetime) -> AbstractContextManager[bool]: ...

    def run_state(
        self, bucket_start: datetime
    ) -> tuple[PortfolioSnapshotRunValue | None, PortfolioSnapshotRunValue | None]: ...

    def save(
        self, run: PortfolioSnapshotRunValue, snapshots: tuple[PortfolioSnapshotValue, ...]
    ) -> PortfolioSnapshotRunValue: ...


class CollectPortfolioSnapshotsUsecase:
    """Hold the collection lock through account reads and the single atomic save."""

    def __init__(
        self,
        collection: PortfolioCollectionPort,
        store: PortfolioCollectionStorePort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._collection = collection
        self._store = store
        self._clock = clock

    async def execute(self) -> PortfolioSnapshotRunResult:
        """Collect once or report the canonical skipped run; release the lock on any failure."""
        captured_at = floor_utc_millisecond(self._clock())
        bucket_start = captured_at.replace(second=0, microsecond=0)
        with self._store.acquire_run_lock(bucket_start) as acquired:
            if not acquired:
                return PortfolioSnapshotRunResult(
                    run_id=None, captured_at=captured_at, saved=0, errors=(), skipped=True
                )
            existing_run, latest_run = self._store.run_state(bucket_start)
            if existing_run is not None:
                return PortfolioSnapshotRunResult(
                    run_id=existing_run.id,
                    captured_at=existing_run.captured_at,
                    saved=0,
                    errors=existing_run.errors,
                    skipped=True,
                )
            if latest_run is not None and latest_run.bucket_start > bucket_start:
                return PortfolioSnapshotRunResult(
                    run_id=latest_run.id,
                    captured_at=latest_run.captured_at,
                    saved=0,
                    errors=latest_run.errors,
                    skipped=True,
                )
            run_id = str(uuid4())
            snapshots, errors = await self._collection.collect(run_id, captured_at, bucket_start)
            run = PortfolioSnapshotRunValue(
                id=run_id, captured_at=captured_at, bucket_start=bucket_start, errors=errors, created_at=captured_at
            )
            persisted_run = self._store.save(run, snapshots)
            if persisted_run.id != run_id:
                return PortfolioSnapshotRunResult(
                    run_id=persisted_run.id,
                    captured_at=persisted_run.captured_at,
                    saved=0,
                    errors=persisted_run.errors,
                    skipped=True,
                )
            return PortfolioSnapshotRunResult(
                run_id=run_id, captured_at=captured_at, saved=len(snapshots), errors=errors, skipped=False
            )
