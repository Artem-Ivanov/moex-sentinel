from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base, PortfolioSnapshotModel, PortfolioSnapshotRunModel
from moex_sentinel.storage.repositories.portfolio_snapshots import (
    PortfolioSnapshotRepository,
    portfolio_snapshot_run_lock,
)
from tests.storage.trading_facts_helpers import user_broker_model

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[tuple[Engine, Session]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(user_broker_model("broker-1", "account-1"))
        session.flush()
        yield engine, session
    engine.dispose()


def run_value(run_id: str, captured_at: datetime, *, with_error: bool = False) -> PortfolioSnapshotRunValue:
    errors = ()
    if with_error:
        errors = (BrokerReadError("broker-1", "Broker", "account-2", "BROKER_UNAVAILABLE", "Недоступно"),)
    return PortfolioSnapshotRunValue(
        id=run_id,
        captured_at=captured_at,
        bucket_start=captured_at.replace(second=0, microsecond=0),
        errors=errors,
        created_at=captured_at,
    )


def snapshot_value(snapshot_id: str, run_id: str, captured_at: datetime) -> PortfolioSnapshotValue:
    return PortfolioSnapshotValue(
        id=snapshot_id,
        run_id=run_id,
        user_broker_id="broker-1",
        account_id="account-1",
        currency="RUB",
        total_value=Decimal("100"),
        free_cash=Decimal("25"),
        cumulative_pnl=Decimal(snapshot_id.removeprefix("snapshot-")),
        captured_at=captured_at,
        bucket_start=captured_at.replace(second=0, microsecond=0),
        created_at=captured_at,
    )


def test_same_run_bucket_is_idempotent_and_preserves_safe_errors(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PortfolioSnapshotRepository(session)
    first_run = run_value("run-1", NOW, with_error=True)
    first_snapshot = snapshot_value("snapshot-1", first_run.id, NOW)

    first = repository.append_run_with_snapshots(first_run, (first_snapshot,))
    second = repository.append_run_with_snapshots(
        run_value("run-2", NOW + timedelta(seconds=30)),
        (snapshot_value("snapshot-2", "run-2", NOW + timedelta(seconds=30)),),
    )

    assert second == first
    assert repository.latest_run() == first_run
    assert repository.latest_snapshots(first_run.id) == (first_snapshot,)
    assert session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)) == 1


def test_baseline_returns_latest_row_at_or_before_target(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PortfolioSnapshotRepository(session)
    snapshots = []
    for minute in range(3):
        captured_at = NOW + timedelta(minutes=minute)
        run = run_value(f"run-{minute}", captured_at)
        value = snapshot_value(f"snapshot-{minute}", run.id, captured_at)
        repository.append_run_with_snapshots(run, (value,))
        snapshots.append(value)

    result = repository.baseline("broker-1", "account-1", "rub", NOW + timedelta(minutes=1, seconds=30))

    assert result == snapshots[1]
    assert repository.latest("broker-1", "account-1", "rub") == snapshots[2]
    assert repository.first("broker-1", "account-1", "rub") == snapshots[0]


def test_sqlite_run_lock_allows_collection(database: tuple[Engine, Session]) -> None:
    engine, _ = database

    with portfolio_snapshot_run_lock(engine, NOW) as acquired:
        assert acquired is True


class ConstraintDiagnostic:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class ConstraintOrigin(Exception):
    def __init__(self, constraint_name: str) -> None:
        self.diag = ConstraintDiagnostic(constraint_name)


class RacingSession:
    def __init__(self, existing: PortfolioSnapshotRunModel, constraint_name: str) -> None:
        self.existing = existing
        self.constraint_name = constraint_name
        self.scalar_calls = 0

    def scalar(self, _statement):
        self.scalar_calls += 1
        return None if self.scalar_calls == 1 else self.existing

    def begin_nested(self):
        return nullcontext()

    def add(self, _model) -> None:
        return None

    def flush(self, _models) -> None:
        raise IntegrityError("INSERT", {}, ConstraintOrigin(self.constraint_name))


def test_concurrent_run_bucket_conflict_returns_the_winning_run() -> None:
    existing_value = run_value("run-existing", NOW, with_error=True)
    existing_model = PortfolioSnapshotRunModel(**existing_value.model_dump(mode="python", exclude={"errors"}))
    existing_model.safe_errors = [error.model_dump(mode="json") for error in existing_value.errors]
    session = RacingSession(existing_model, "uq_portfolio_snapshot_runs_bucket")
    repository = PortfolioSnapshotRepository(session)  # type: ignore[arg-type]

    result = repository.append_run_with_snapshots(run_value("run-racing", NOW), ())

    assert result == existing_value


def test_concurrent_insert_does_not_hide_an_unrelated_constraint_error() -> None:
    existing_value = run_value("run-existing", NOW)
    existing_model = PortfolioSnapshotRunModel(**existing_value.model_dump(mode="python", exclude={"errors"}))
    existing_model.safe_errors = []
    session = RacingSession(existing_model, "other_constraint")
    repository = PortfolioSnapshotRepository(session)  # type: ignore[arg-type]

    with pytest.raises(IntegrityError):
        repository.append_run_with_snapshots(run_value("run-racing", NOW), ())
