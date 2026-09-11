from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.services.trading_summary import TradingSummaryService
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from tests.storage.trading_facts_helpers import user_broker_model

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


@pytest.fixture
def repository() -> Iterator[PortfolioSnapshotRepository]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            (
                user_broker_model("broker-1", "account-1"),
                user_broker_model("broker-2", "account-2"),
                user_broker_model("broker-3", "account-3"),
            )
        )
        session.flush()
        yield PortfolioSnapshotRepository(session)
    engine.dispose()


def append_run(
    repository: PortfolioSnapshotRepository,
    captured_at: datetime,
    values: tuple[tuple[str, str, str, str, str, str], ...],
    *,
    errors: tuple[BrokerReadError, ...] = (),
) -> None:
    run_id = f"run-{captured_at.isoformat()}"
    run = PortfolioSnapshotRunValue(
        run_id,
        captured_at,
        captured_at.replace(second=0, microsecond=0),
        errors,
        captured_at,
    )
    snapshots = tuple(
        PortfolioSnapshotValue(
            id=f"snapshot-{run_id}-{broker_id}",
            run_id=run_id,
            user_broker_id=broker_id,
            account_id=account_id,
            currency=currency,
            total_value=Decimal(total),
            free_cash=Decimal(free_cash),
            cumulative_pnl=Decimal(cumulative_pnl),
            captured_at=captured_at,
            bucket_start=captured_at.replace(second=0, microsecond=0),
            created_at=captured_at,
        )
        for broker_id, account_id, currency, total, free_cash, cumulative_pnl in values
    )
    repository.append_run_with_snapshots(run, snapshots)


def test_empty_history_returns_empty_summary(repository: PortfolioSnapshotRepository) -> None:
    result = TradingSummaryService(repository).view()

    assert result.captured_at is None
    assert result.currencies == ()
    assert result.errors == ()


def test_summary_aggregates_accounts_by_currency(repository: PortfolioSnapshotRepository) -> None:
    append_run(
        repository,
        NOW - timedelta(days=30),
        (
            ("broker-1", "account-1", "RUB", "80", "15", "0"),
            ("broker-2", "account-2", "RUB", "45", "5", "0"),
            ("broker-3", "account-3", "USD", "18", "2", "0"),
        ),
    )
    append_run(
        repository,
        NOW - timedelta(days=7),
        (
            ("broker-1", "account-1", "RUB", "90", "18", "2"),
            ("broker-2", "account-2", "RUB", "47", "5", "1"),
            ("broker-3", "account-3", "USD", "19", "2", "0"),
        ),
    )
    append_run(
        repository,
        NOW - timedelta(days=1),
        (
            ("broker-1", "account-1", "RUB", "95", "19", "4"),
            ("broker-2", "account-2", "RUB", "48", "5", "4"),
            ("broker-3", "account-3", "USD", "19", "2", "1"),
        ),
    )
    append_run(
        repository,
        NOW,
        (
            ("broker-1", "account-1", "RUB", "100", "20", "10"),
            ("broker-2", "account-2", "RUB", "50", "5", "5"),
            ("broker-3", "account-3", "USD", "20", "3", "3"),
        ),
    )

    result = TradingSummaryService(repository).view()

    rub, usd = result.currencies
    assert result.captured_at == NOW
    assert (rub.currency, rub.portfolio_value, rub.free_cash) == ("RUB", Decimal("150"), Decimal("25"))
    assert (rub.pnl_24h.value, rub.pnl_7d.value, rub.pnl_30d.value) == (
        Decimal("7"),
        Decimal("12"),
        Decimal("15"),
    )
    assert rub.pnl_30d.complete is True
    assert (usd.currency, usd.portfolio_value, usd.pnl_24h.value) == ("USD", Decimal("20"), Decimal("2"))


def test_summary_uses_common_actual_start_for_incomplete_accounts(
    repository: PortfolioSnapshotRepository,
) -> None:
    append_run(
        repository,
        NOW - timedelta(days=7),
        (("broker-1", "account-1", "RUB", "90", "15", "2"),),
    )
    append_run(
        repository,
        NOW - timedelta(days=2),
        (
            ("broker-1", "account-1", "RUB", "95", "18", "5"),
            ("broker-2", "account-2", "RUB", "45", "5", "1"),
        ),
    )
    append_run(
        repository,
        NOW,
        (
            ("broker-1", "account-1", "RUB", "100", "20", "10"),
            ("broker-2", "account-2", "RUB", "50", "5", "4"),
        ),
    )

    period = TradingSummaryService(repository).view().currencies[0].pnl_7d

    assert period.value == Decimal("8")
    assert period.from_at == NOW - timedelta(days=2)
    assert period.to_at == NOW
    assert period.complete is False


def test_summary_returns_safe_errors_from_latest_run(repository: PortfolioSnapshotRepository) -> None:
    error = BrokerReadError("broker-1", "Broker", "account-1", "BROKER_UNAVAILABLE", "Недоступно")
    append_run(repository, NOW, (), errors=(error,))

    result = TradingSummaryService(repository).view()

    assert result.currencies == ()
    assert result.errors == (error,)


def test_summary_uses_one_common_run_when_account_missed_requested_boundary(
    repository: PortfolioSnapshotRepository,
) -> None:
    append_run(
        repository,
        NOW - timedelta(days=2),
        (
            ("broker-1", "account-1", "RUB", "90", "15", "2"),
            ("broker-2", "account-2", "RUB", "45", "5", "1"),
        ),
    )
    append_run(
        repository,
        NOW - timedelta(days=1),
        (("broker-1", "account-1", "RUB", "95", "18", "5"),),
    )
    append_run(
        repository,
        NOW,
        (
            ("broker-1", "account-1", "RUB", "100", "20", "10"),
            ("broker-2", "account-2", "RUB", "50", "5", "4"),
        ),
    )

    period = TradingSummaryService(repository).view().currencies[0].pnl_24h

    assert period.value == Decimal("11")
    assert period.from_at == NOW - timedelta(days=2)
    assert period.complete is True
