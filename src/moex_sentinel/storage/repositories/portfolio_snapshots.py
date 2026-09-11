"""Persistence for portfolio collection runs and historical snapshots."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import blake2b

from sqlalchemy import Engine, func, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.storage.models.trading_analytics import PortfolioSnapshotModel, PortfolioSnapshotRunModel
from sentinel_contracts.time import floor_utc_millisecond


def _run_value(model: PortfolioSnapshotRunModel) -> PortfolioSnapshotRunValue:
    return PortfolioSnapshotRunValue(
        id=model.id,
        captured_at=model.captured_at,
        bucket_start=model.bucket_start,
        errors=tuple(BrokerReadError.model_validate(item) for item in model.safe_errors),
        created_at=model.created_at,
    )


def _snapshot_value(model: PortfolioSnapshotModel) -> PortfolioSnapshotValue:
    return PortfolioSnapshotValue(
        id=model.id,
        run_id=model.run_id,
        user_broker_id=model.user_broker_id,
        account_id=model.account_id,
        currency=model.currency,
        total_value=model.total_value,
        free_cash=model.free_cash,
        cumulative_pnl=model.cumulative_pnl,
        captured_at=model.captured_at,
        bucket_start=model.bucket_start,
        created_at=model.created_at,
    )


_COLLECTION_LOCK_KEY = int.from_bytes(
    blake2b(b"moex-sentinel:portfolio-snapshot-collection", digest_size=8).digest(),
    "big",
    signed=True,
)


def _is_run_bucket_conflict(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == "uq_portfolio_snapshot_runs_bucket"
    message = str(error.orig).lower()
    return "unique constraint failed: portfolio_snapshot_runs.bucket_start" in message


@contextmanager
def portfolio_snapshot_run_lock(engine: Engine, bucket_start: datetime) -> Iterator[bool]:
    """Hold the single PostgreSQL session lock for snapshot collection."""
    del bucket_start
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as connection:
        acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _COLLECTION_LOCK_KEY}))
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _COLLECTION_LOCK_KEY},
                )


class PortfolioSnapshotRepository:
    """Session-bound atomic snapshot repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_run_with_snapshots(
        self,
        run: PortfolioSnapshotRunValue,
        snapshots: tuple[PortfolioSnapshotValue, ...],
    ) -> PortfolioSnapshotRunValue:
        existing = self._session.scalar(
            select(PortfolioSnapshotRunModel).where(PortfolioSnapshotRunModel.bucket_start == run.bucket_start)
        )
        if existing is not None:
            return _run_value(existing)
        if any(value.run_id != run.id for value in snapshots):
            raise ValueError("Every snapshot must belong to the appended run.")
        run_model = PortfolioSnapshotRunModel(
            id=run.id,
            captured_at=run.captured_at,
            bucket_start=run.bucket_start,
            safe_errors=[error.model_dump(mode="json") for error in run.errors],
            created_at=run.created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(run_model)
                self._session.flush((run_model,))
        except IntegrityError as error:
            if not _is_run_bucket_conflict(error):
                raise
            existing = self._session.scalar(
                select(PortfolioSnapshotRunModel).where(PortfolioSnapshotRunModel.bucket_start == run.bucket_start)
            )
            if existing is None:
                raise
            return _run_value(existing)
        self._session.add_all(PortfolioSnapshotModel(**value.model_dump(mode="python")) for value in snapshots)
        self._session.flush()
        return run

    def latest_run(self) -> PortfolioSnapshotRunValue | None:
        model = self._session.scalar(
            select(PortfolioSnapshotRunModel).order_by(PortfolioSnapshotRunModel.captured_at.desc()).limit(1)
        )
        return None if model is None else _run_value(model)

    def run_for_bucket(self, bucket_start: datetime) -> PortfolioSnapshotRunValue | None:
        model = self._session.scalar(
            select(PortfolioSnapshotRunModel).where(
                PortfolioSnapshotRunModel.bucket_start == floor_utc_millisecond(bucket_start)
            )
        )
        return None if model is None else _run_value(model)

    def latest_snapshots(self, run_id: str) -> tuple[PortfolioSnapshotValue, ...]:
        models = self._session.scalars(
            select(PortfolioSnapshotModel)
            .where(PortfolioSnapshotModel.run_id == run_id)
            .order_by(
                PortfolioSnapshotModel.currency,
                PortfolioSnapshotModel.user_broker_id,
                PortfolioSnapshotModel.account_id,
            )
        )
        return tuple(_snapshot_value(model) for model in models)

    def latest(self, user_broker_id: str, account_id: str, currency: str) -> PortfolioSnapshotValue | None:
        model = self._session.scalar(
            self._account_currency_query(user_broker_id, account_id, currency)
            .order_by(PortfolioSnapshotModel.captured_at.desc())
            .limit(1)
        )
        return None if model is None else _snapshot_value(model)

    def baseline(
        self,
        user_broker_id: str,
        account_id: str,
        currency: str,
        at_or_before: datetime,
    ) -> PortfolioSnapshotValue | None:
        model = self._session.scalar(
            self._account_currency_query(user_broker_id, account_id, currency)
            .where(PortfolioSnapshotModel.captured_at <= floor_utc_millisecond(at_or_before))
            .order_by(PortfolioSnapshotModel.captured_at.desc())
            .limit(1)
        )
        return None if model is None else _snapshot_value(model)

    def first(self, user_broker_id: str, account_id: str, currency: str) -> PortfolioSnapshotValue | None:
        model = self._session.scalar(
            self._account_currency_query(user_broker_id, account_id, currency)
            .order_by(PortfolioSnapshotModel.captured_at)
            .limit(1)
        )
        return None if model is None else _snapshot_value(model)

    def common_baselines(
        self,
        latest: tuple[PortfolioSnapshotValue, ...],
        at_or_before: datetime,
    ) -> tuple[PortfolioSnapshotValue, ...]:
        """Select one historical run containing every requested account."""
        if not latest:
            return ()
        currencies = {snapshot.currency for snapshot in latest}
        if len(currencies) != 1:
            raise ValueError("Common baselines must use one currency.")
        identities = {(snapshot.user_broker_id, snapshot.account_id) for snapshot in latest}
        if len(identities) != len(latest):
            raise ValueError("Common baselines require unique broker accounts.")

        identity_filter = tuple_(
            PortfolioSnapshotModel.user_broker_id,
            PortfolioSnapshotModel.account_id,
        ).in_(identities)
        common_runs = (
            select(PortfolioSnapshotModel.run_id, PortfolioSnapshotModel.captured_at)
            .where(
                PortfolioSnapshotModel.currency == next(iter(currencies)),
                identity_filter,
            )
            .group_by(PortfolioSnapshotModel.run_id, PortfolioSnapshotModel.captured_at)
            .having(func.count(PortfolioSnapshotModel.id) == len(identities))
        )
        requested = floor_utc_millisecond(at_or_before)
        run_id = self._session.scalar(
            common_runs.where(PortfolioSnapshotModel.captured_at <= requested)
            .order_by(PortfolioSnapshotModel.captured_at.desc())
            .limit(1)
        )
        if run_id is None:
            run_id = self._session.scalar(common_runs.order_by(PortfolioSnapshotModel.captured_at).limit(1))
        if run_id is None:
            return ()
        models = self._session.scalars(
            select(PortfolioSnapshotModel)
            .where(
                PortfolioSnapshotModel.run_id == run_id,
                PortfolioSnapshotModel.currency == next(iter(currencies)),
                identity_filter,
            )
            .order_by(PortfolioSnapshotModel.user_broker_id, PortfolioSnapshotModel.account_id)
        )
        return tuple(_snapshot_value(model) for model in models)

    @staticmethod
    def _account_currency_query(user_broker_id: str, account_id: str, currency: str):
        return select(PortfolioSnapshotModel).where(
            PortfolioSnapshotModel.user_broker_id == user_broker_id,
            PortfolioSnapshotModel.account_id == account_id,
            PortfolioSnapshotModel.currency == currency.upper(),
        )
