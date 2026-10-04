"""Persistence for portfolio collection runs and historical snapshots."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import blake2b

from sqlalchemy import Engine, func, literal, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.storage.models.trading_analytics import PortfolioSnapshotModel, PortfolioSnapshotRunModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
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

    def __init__(self, session: Session, *, environment: str | None = None) -> None:
        self._session = session
        self._environment = environment

    def _run_value(self, model: PortfolioSnapshotRunModel) -> PortfolioSnapshotRunValue:
        run = _run_value(model)
        if self._environment is None or not run.errors:
            return run
        broker_ids = set(
            self._session.scalars(select(UserBrokerModel.id).where(UserBrokerModel.environment == self._environment))
        )
        return run.model_copy(update={"errors": tuple(error for error in run.errors if error.broker_id in broker_ids)})

    def _scope_query(self, statement):
        if self._environment is None:
            return statement
        return statement.join(UserBrokerModel, UserBrokerModel.id == PortfolioSnapshotModel.user_broker_id).where(
            UserBrokerModel.environment == self._environment
        )

    def append_run_with_snapshots(
        self,
        run: PortfolioSnapshotRunValue,
        snapshots: tuple[PortfolioSnapshotValue, ...],
    ) -> PortfolioSnapshotRunValue:
        existing = self._session.scalar(
            select(PortfolioSnapshotRunModel).where(PortfolioSnapshotRunModel.bucket_start == run.bucket_start)
        )
        if existing is not None:
            return self._run_value(existing)
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
            return self._run_value(existing)
        self._session.add_all(PortfolioSnapshotModel(**value.model_dump(mode="python")) for value in snapshots)
        self._session.flush()
        return run

    def latest_run(self) -> PortfolioSnapshotRunValue | None:
        model = self._session.scalar(
            select(PortfolioSnapshotRunModel).order_by(PortfolioSnapshotRunModel.captured_at.desc()).limit(1)
        )
        return None if model is None else self._run_value(model)

    def run_for_bucket(self, bucket_start: datetime) -> PortfolioSnapshotRunValue | None:
        model = self._session.scalar(
            select(PortfolioSnapshotRunModel).where(
                PortfolioSnapshotRunModel.bucket_start == floor_utc_millisecond(bucket_start)
            )
        )
        return None if model is None else self._run_value(model)

    def latest_snapshots(self, run_id: str) -> tuple[PortfolioSnapshotValue, ...]:
        models = self._session.scalars(
            self._scope_query(select(PortfolioSnapshotModel))
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
            self._scope_query(select(PortfolioSnapshotModel.run_id, PortfolioSnapshotModel.captured_at))
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
            self._scope_query(select(PortfolioSnapshotModel))
            .where(
                PortfolioSnapshotModel.run_id == run_id,
                PortfolioSnapshotModel.currency == next(iter(currencies)),
                identity_filter,
            )
            .order_by(PortfolioSnapshotModel.user_broker_id, PortfolioSnapshotModel.account_id)
        )
        return tuple(_snapshot_value(model) for model in models)

    def common_baselines_many(
        self,
        latest: tuple[PortfolioSnapshotValue, ...],
        boundaries: tuple[datetime, ...],
    ) -> tuple[tuple[PortfolioSnapshotValue, ...], ...]:
        """Select up to three common historical runs in one query."""
        if len(boundaries) > 3:
            raise ValueError("Common baselines support at most three boundaries.")
        if not boundaries:
            return ()
        if not latest:
            return tuple(() for _ in boundaries)
        currencies = {snapshot.currency for snapshot in latest}
        if len(currencies) != 1:
            raise ValueError("Common baselines must use one currency.")
        identities = {(snapshot.user_broker_id, snapshot.account_id) for snapshot in latest}
        if len(identities) != len(latest):
            raise ValueError("Common baselines require unique broker accounts.")
        currency = next(iter(currencies))
        identity_filter = tuple_(
            PortfolioSnapshotModel.user_broker_id,
            PortfolioSnapshotModel.account_id,
        ).in_(identities)
        common_query = (
            self._scope_query(select(PortfolioSnapshotModel.run_id, PortfolioSnapshotModel.captured_at))
            .where(PortfolioSnapshotModel.currency == currency, identity_filter)
            .group_by(PortfolioSnapshotModel.run_id, PortfolioSnapshotModel.captured_at)
            .having(func.count(PortfolioSnapshotModel.id) == len(identities))
        )
        window_queries = tuple(
            select(
                literal(index).label("ordinal"),
                literal(floor_utc_millisecond(boundary), type_=PortfolioSnapshotModel.captured_at.type).label("cutoff"),
            )
            for index, boundary in enumerate(boundaries)
        )
        windows = window_queries[0].union_all(*window_queries[1:]).cte("baseline_windows")
        common_runs = common_query.cte("common_runs")
        requested_run = (
            select(common_runs.c.run_id)
            .where(common_runs.c.captured_at <= windows.c.cutoff)
            .order_by(common_runs.c.captured_at.desc())
            .limit(1)
            .correlate(windows)
            .scalar_subquery()
        )
        first_run = select(common_runs.c.run_id).order_by(common_runs.c.captured_at).limit(1).scalar_subquery()
        if self._session.get_bind().dialect.name == "postgresql":
            # Keep GROUP/ORDER/LIMIT together so PostgreSQL can stop at the first complete run.
            grouped_ids = common_query.with_only_columns(PortfolioSnapshotModel.run_id)
            requested_run = (
                grouped_ids.where(PortfolioSnapshotModel.captured_at <= windows.c.cutoff)
                .order_by(PortfolioSnapshotModel.captured_at.desc())
                .limit(1)
                .correlate(windows)
                .scalar_subquery()
            )
            first_run = grouped_ids.order_by(PortfolioSnapshotModel.captured_at).limit(1).scalar_subquery()
        selected = self._session.execute(
            select(windows.c.ordinal, func.coalesce(requested_run, first_run).label("run_id"))
            .select_from(windows)
            .order_by(windows.c.ordinal)
        ).all()
        run_ids = {row.run_id for row in selected if row.run_id is not None}
        if not run_ids:
            return tuple(() for _ in boundaries)
        models = self._session.scalars(
            self._scope_query(select(PortfolioSnapshotModel))
            .where(
                PortfolioSnapshotModel.run_id.in_(run_ids),
                PortfolioSnapshotModel.currency == currency,
                identity_filter,
            )
            .order_by(
                PortfolioSnapshotModel.run_id,
                PortfolioSnapshotModel.user_broker_id,
                PortfolioSnapshotModel.account_id,
            )
        )
        by_run: dict[str, list[PortfolioSnapshotValue]] = {run_id: [] for run_id in run_ids}
        for model in models:
            by_run[model.run_id].append(_snapshot_value(model))
        return tuple(tuple(by_run.get(row.run_id, ())) for row in selected)

    def _account_currency_query(self, user_broker_id: str, account_id: str, currency: str):
        return self._scope_query(select(PortfolioSnapshotModel)).where(
            PortfolioSnapshotModel.user_broker_id == user_broker_id,
            PortfolioSnapshotModel.account_id == account_id,
            PortfolioSnapshotModel.currency == currency.upper(),
        )
