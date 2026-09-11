import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker, BrokerField
from moex_sentinel.domain.portfolio import AccountPortfolio, BrokerAccount, ExternalOperation, Money, OperationsPage
from moex_sentinel.domain.user_brokers import UserBroker, UserBrokerState
from moex_sentinel.services.portfolio_snapshot_collection import PortfolioSnapshotCollector
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base, PortfolioSnapshotModel
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from tests.storage.trading_facts_helpers import user_broker_model

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


class BrokerRepository:
    def __init__(self, records: list[Broker | UserBroker]) -> None:
        self.records = records

    def list(self) -> list[Broker | UserBroker]:
        return self.records


class PortfolioAdapter:
    def __init__(self) -> None:
        self.total_value = Decimal("100")
        self.operations: tuple[ExternalOperation, ...] = ()
        self.pages: dict[str | None, OperationsPage] | None = None

    async def list_accounts(self) -> tuple[BrokerAccount, ...]:
        return (BrokerAccount("account-1", "Main", "OPEN", "BROKER"),)

    async def get_portfolio(self, account_id: str) -> AccountPortfolio:
        return AccountPortfolio(
            account_id,
            Money(self.total_value, "RUB"),
            Money(Decimal("25"), "RUB"),
            None,
            None,
        )

    async def get_operations(
        self,
        account_id: str,
        cursor: str | None,
        limit: int,
        instrument_id: str | None = None,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> OperationsPage:
        if self.pages is not None:
            return self.pages[cursor]
        return OperationsPage(self.operations, None)


@pytest.fixture
def database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(user_broker_model("broker-1", "account-1"))
        session.commit()
    yield engine, factory
    engine.dispose()


def broker(broker_id: str = "broker-1", account_id: str = "account-1") -> Broker:
    return Broker(
        id=broker_id,
        display_name=f"Broker {broker_id}",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=True,
        fields=(BrokerField("token", "synthetic-token"),),
        created_at=NOW,
        updated_at=NOW,
        account_id=account_id,
    )


def test_first_snapshot_starts_at_zero(database: tuple[Engine, sessionmaker[Session]]) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    collector = PortfolioSnapshotCollector(
        BrokerRepository([broker()]),
        lambda _broker: adapter,
        factory,
        engine,
        clock=lambda: NOW,
    )

    result = asyncio.run(collector.collect_once())

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert result.saved == 1
    assert saved is not None
    assert saved.cumulative_pnl == Decimal("0")


def external_operation(operation_id: str, operation_type: str, amount: str) -> ExternalOperation:
    return ExternalOperation(
        operation_id=operation_id,
        account_id="account-1",
        operation_type=operation_type,
        state="OPERATION_STATE_EXECUTED",
        occurred_at=NOW + timedelta(seconds=30),
        payment=Money(Decimal(amount), "RUB"),
        price=None,
        quantity=Decimal(),
        commission=None,
    )


def test_next_snapshot_excludes_cash_movements_but_includes_commission_loss(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    first = PortfolioSnapshotCollector(
        BrokerRepository([broker()]), lambda _broker: adapter, factory, engine, clock=lambda: NOW
    )
    asyncio.run(first.collect_once())
    adapter.total_value = Decimal("173")
    adapter.operations = (
        external_operation("deposit", "OPERATION_TYPE_INPUT", "100"),
        external_operation("withdrawal", "OPERATION_TYPE_OUTPUT", "-25"),
        external_operation("commission", "OPERATION_TYPE_BROKER_FEE", "-2"),
    )
    second = PortfolioSnapshotCollector(
        BrokerRepository([broker()]),
        lambda _broker: adapter,
        factory,
        engine,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    asyncio.run(second.collect_once())

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert saved is not None
    assert saved.cumulative_pnl == Decimal("-2")


def test_previous_capture_boundary_operation_is_not_counted_twice(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    repository = BrokerRepository([broker()])
    asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    adapter.operations = (external_operation("boundary", "OPERATION_TYPE_INPUT", "100"),)
    adapter.operations = (adapter.operations[0].model_copy(update={"occurred_at": NOW}),)

    asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=1),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert saved is not None
    assert saved.cumulative_pnl == Decimal("0")


def test_cash_movements_are_read_from_every_operations_page(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker()]), lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    adapter.total_value = Decimal("173")
    adapter.pages = {
        None: OperationsPage((external_operation("deposit", "OPERATION_TYPE_INPUT", "100"),), "next"),
        "next": OperationsPage((external_operation("withdrawal", "OPERATION_TYPE_OUTPUT", "-25"),), None),
    }

    asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker()]),
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=1),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert saved is not None
    assert saved.cumulative_pnl == Decimal("-2")


def test_unreadable_portfolio_skips_only_that_account(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class PartiallyFailingAdapter(PortfolioAdapter):
        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            return (
                BrokerAccount("account-1", "Healthy", "OPEN", "BROKER"),
                BrokerAccount("account-2", "Broken", "OPEN", "BROKER"),
            )

        async def get_portfolio(self, account_id: str) -> AccountPortfolio:
            if account_id == "account-2":
                raise TInvestAdapterError("BROKER_UNAVAILABLE", "Площадка недоступна.", retryable=False)
            return await super().get_portfolio(account_id)

    class HealthyAdapter(PortfolioAdapter):
        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            return (BrokerAccount("account-3", "Healthy", "OPEN", "BROKER"),)

    engine, factory = database
    with factory() as session:
        session.add(user_broker_model("broker-2", "account-3"))
        session.commit()
    adapters = {
        "broker-1": PartiallyFailingAdapter(),
        "broker-2": HealthyAdapter(),
    }
    collector = PortfolioSnapshotCollector(
        BrokerRepository([broker("broker-1", "account-2"), broker("broker-2", "account-3")]),
        lambda value: adapters[value.id],
        factory,
        engine,
        clock=lambda: NOW,
    )

    result = asyncio.run(collector.collect_once())

    with factory() as session:
        repository = PortfolioSnapshotRepository(session)
        run = repository.latest_run()
        healthy = repository.latest("broker-2", "account-3", "RUB")
        broken = repository.latest("broker-1", "account-2", "RUB")
    assert result.saved == 1
    assert result.errors[0].account_id == "account-2"
    assert run is not None
    assert run.errors == result.errors
    assert healthy is not None
    assert broken is None


def test_unreadable_cash_operations_do_not_create_a_new_snapshot(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class OperationsFailingAdapter(PortfolioAdapter):
        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
            *,
            from_at: datetime | None = None,
            to_at: datetime | None = None,
        ) -> OperationsPage:
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Операции недоступны.", retryable=False)

    engine, factory = database
    adapter = OperationsFailingAdapter()
    repository = BrokerRepository([broker()])
    asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    adapter.total_value = Decimal("101")

    result = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=1),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert result.saved == 0
    assert result.errors[0].account_id == "account-1"
    assert saved is not None
    assert saved.captured_at == NOW


def test_retryable_broker_error_uses_bounded_exponential_backoff(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class RetryAdapter(PortfolioAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def get_portfolio(self, account_id: str) -> AccountPortfolio:
            self.attempts += 1
            if self.attempts < 3:
                raise TInvestAdapterError("BROKER_UNAVAILABLE", "Площадка недоступна.", retryable=True)
            return await super().get_portfolio(account_id)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    engine, factory = database
    adapter = RetryAdapter()
    delays: list[float] = []
    collector = PortfolioSnapshotCollector(
        BrokerRepository([broker()]),
        lambda _broker: adapter,
        factory,
        engine,
        clock=lambda: NOW,
        retry_limit=2,
        retry_base_seconds=0.5,
        sleep=record_sleep,
    )

    result = asyncio.run(collector.collect_once())

    assert result.saved == 1
    assert adapter.attempts == 3
    assert delays == [0.5, 1.0]


def test_retryable_operations_page_is_retried_before_skipping_account(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class RetryOperationsAdapter(PortfolioAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.operation_attempts = 0

        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
            *,
            from_at: datetime | None = None,
            to_at: datetime | None = None,
        ) -> OperationsPage:
            self.operation_attempts += 1
            if self.operation_attempts == 1:
                raise TInvestAdapterError("BROKER_UNAVAILABLE", "Операции недоступны.", retryable=True)
            return OperationsPage((), None)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    engine, factory = database
    adapter = RetryOperationsAdapter()
    repository = BrokerRepository([broker()])
    asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    adapter.total_value = Decimal("101")
    delays: list[float] = []
    collector = PortfolioSnapshotCollector(
        repository,
        lambda _broker: adapter,
        factory,
        engine,
        clock=lambda: NOW + timedelta(minutes=1),
        retry_limit=1,
        retry_base_seconds=0.25,
        sleep=record_sleep,
    )

    result = asyncio.run(collector.collect_once())

    assert result.saved == 1
    assert result.errors == ()
    assert adapter.operation_attempts == 2
    assert delays == [0.25]


def test_unreadable_broker_account_list_does_not_block_other_brokers(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class FailingAccountsAdapter(PortfolioAdapter):
        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Счета недоступны.", retryable=False)

    engine, factory = database
    with factory() as session:
        session.add(user_broker_model("broker-2", "account-2"))
        session.commit()
    adapters = {"broker-1": FailingAccountsAdapter(), "broker-2": PortfolioAdapter()}
    collector = PortfolioSnapshotCollector(
        BrokerRepository([broker("broker-1"), broker("broker-2")]),
        lambda value: adapters[value.id],
        factory,
        engine,
        clock=lambda: NOW,
    )

    result = asyncio.run(collector.collect_once())

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-2", "account-1", "RUB")
    assert result.saved == 1
    assert result.errors[0].broker_id == "broker-1"
    assert result.errors[0].account_id is None
    assert saved is not None


def test_invalid_broker_configuration_does_not_block_other_brokers(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    with factory() as session:
        session.add(user_broker_model("broker-2", "account-2"))
        session.commit()

    def adapter_factory(value: Broker) -> PortfolioAdapter:
        if value.id == "broker-1":
            raise ValueError("Некорректная конфигурация.")
        return PortfolioAdapter()

    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker("broker-1"), broker("broker-2")]),
            adapter_factory,
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    assert result.saved == 1
    assert result.errors[0].broker_id == "broker-1"
    assert result.errors[0].code == "BROKER_CONFIGURATION"


def test_incompatible_portfolio_currency_is_reported_and_not_persisted(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class MixedCurrencyAdapter(PortfolioAdapter):
        async def get_portfolio(self, account_id: str) -> AccountPortfolio:
            return AccountPortfolio(
                account_id,
                Money(Decimal("100"), "RUB"),
                Money(Decimal("25"), "USD"),
                None,
                None,
            )

    engine, factory = database
    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker()]),
            lambda _broker: MixedCurrencyAdapter(),
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    assert result.saved == 0
    assert result.errors[0].code == "INVALID_PORTFOLIO_SNAPSHOT"
    assert result.errors[0].account_id == "account-1"


def test_cash_movement_after_run_timestamp_rejects_first_snapshot(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    adapter.total_value = Decimal("200")
    adapter.operations = (external_operation("late-deposit", "OPERATION_TYPE_INPUT", "100"),)
    moments = iter((NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=40)))

    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker()]),
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: next(moments),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert result.saved == 0
    assert result.errors[0].code == "AMBIGUOUS_PORTFOLIO_SNAPSHOT"
    assert saved is None


def test_cash_movement_after_run_timestamp_rejects_followup_snapshot(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    repository = BrokerRepository([broker()])
    asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    adapter.total_value = Decimal("200")
    adapter.operations = (
        external_operation("late-deposit", "OPERATION_TYPE_INPUT", "100").model_copy(
            update={"occurred_at": NOW + timedelta(minutes=1, seconds=20)}
        ),
    )
    run_at = NOW + timedelta(minutes=1)
    moments = iter((run_at, run_at + timedelta(seconds=5), run_at + timedelta(seconds=30)))

    result = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: next(moments),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert result.saved == 0
    assert result.errors[0].code == "AMBIGUOUS_PORTFOLIO_SNAPSHOT"
    assert saved is not None
    assert saved.captured_at == NOW


@pytest.mark.parametrize(
    ("total_value", "free_cash"),
    [
        (Decimal("-1"), Decimal("25")),
        (Decimal("10000000000000000000"), Decimal("25")),
        (Decimal("100"), Decimal("-1")),
        (Decimal("100.0000000001"), Decimal("25")),
    ],
)
def test_unstorable_account_amount_does_not_block_valid_account(
    database: tuple[Engine, sessionmaker[Session]],
    total_value: Decimal,
    free_cash: Decimal,
) -> None:
    class AmountAdapter(PortfolioAdapter):
        def __init__(self, account_id: str, total: Decimal, cash: Decimal) -> None:
            super().__init__()
            self.account_id = account_id
            self.total = total
            self.cash = cash

        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            return (BrokerAccount(self.account_id, "Main", "OPEN", "BROKER"),)

        async def get_portfolio(self, account_id: str) -> AccountPortfolio:
            return AccountPortfolio(
                account_id,
                Money(self.total, "RUB"),
                Money(self.cash, "RUB"),
                None,
                None,
            )

    engine, factory = database
    with factory() as session:
        session.add(user_broker_model("broker-2", "account-2"))
        session.commit()
    adapters = {
        "broker-1": AmountAdapter("account-1", total_value, free_cash),
        "broker-2": AmountAdapter("account-2", Decimal("100"), Decimal("25")),
    }

    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker("broker-1"), broker("broker-2", "account-2")]),
            lambda value: adapters[value.id],
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    with factory() as session:
        invalid = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
        valid = PortfolioSnapshotRepository(session).latest("broker-2", "account-2", "RUB")
    assert result.saved == 1
    assert result.errors[0].code == "INVALID_PORTFOLIO_SNAPSHOT"
    assert invalid is None
    assert valid is not None


def test_repeated_collection_in_same_minute_reports_skip_without_duplicates(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    repository = BrokerRepository([broker()])
    first = asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    second = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(seconds=30),
        ).collect_once()
    )

    with factory() as session:
        count = session.scalar(select(func.count()).select_from(PortfolioSnapshotModel))
    assert first.saved == 1
    assert second.run_id == first.run_id
    assert second.saved == 0
    assert second.skipped is True
    assert count == 1


def test_stale_bucket_returns_the_existing_run_without_collecting_duplicate(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    repository = BrokerRepository([broker()])
    first = asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=1),
        ).collect_once()
    )
    adapter.total_value = Decimal("999")

    stale = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(seconds=30),
        ).collect_once()
    )

    with factory() as session:
        snapshots = PortfolioSnapshotRepository(session).latest_snapshots(first.run_id or "")
    assert stale.run_id == first.run_id
    assert stale.captured_at == first.captured_at
    assert stale.saved == 0
    assert stale.skipped is True
    assert snapshots[0].total_value == Decimal("100")


def test_unseen_stale_bucket_returns_latest_run_without_building_from_future(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    adapter = PortfolioAdapter()
    repository = BrokerRepository([broker()])
    asyncio.run(
        PortfolioSnapshotCollector(
            repository, lambda _broker: adapter, factory, engine, clock=lambda: NOW
        ).collect_once()
    )
    latest = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=2),
        ).collect_once()
    )
    adapter.total_value = Decimal("999")

    stale = asyncio.run(
        PortfolioSnapshotCollector(
            repository,
            lambda _broker: adapter,
            factory,
            engine,
            clock=lambda: NOW + timedelta(minutes=1),
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert stale.run_id == latest.run_id
    assert stale.captured_at == latest.captured_at
    assert stale.saved == 0
    assert stale.skipped is True
    assert saved is not None
    assert saved.total_value == Decimal("100")


def test_collection_only_reads_the_configured_external_account(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class MultiAccountAdapter(PortfolioAdapter):
        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            return (
                BrokerAccount("account-1", "Selected", "OPEN", "BROKER"),
                BrokerAccount("account-foreign", "Foreign", "OPEN", "BROKER"),
            )

    engine, factory = database
    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([broker(account_id="account-1")]),
            lambda _broker: MultiAccountAdapter(),
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    with factory() as session:
        selected = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
        foreign = PortfolioSnapshotRepository(session).latest("broker-1", "account-foreign", "RUB")
    assert result.saved == 1
    assert selected is not None
    assert foreign is None


def test_draft_user_broker_is_not_collected(database: tuple[Engine, sessionmaker[Session]]) -> None:
    engine, factory = database
    draft = UserBroker(
        api_slug="TINVEST_SANDBOX",
        display_name="Draft",
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id="account-1",
        state=UserBrokerState.DRAFT,
        id="broker-1",
        created_at=NOW,
        updated_at=NOW,
    )

    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([draft]),
            lambda _broker: PortfolioAdapter(),
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    assert result.saved == 0


def test_active_production_user_broker_collects_its_external_account(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = database
    active = UserBroker(
        api_slug="TINVEST_SANDBOX",
        display_name="Active",
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id="account-1",
        state=UserBrokerState.ACTIVE,
        id="broker-1",
        created_at=NOW,
        updated_at=NOW,
    )

    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository([active]),
            lambda _broker: PortfolioAdapter(),
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    with factory() as session:
        saved = PortfolioSnapshotRepository(session).latest("broker-1", "account-1", "RUB")
    assert result.saved == 1
    assert saved is not None


def test_two_scopes_with_shared_account_visibility_are_not_double_counted(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    class SharedVisibilityAdapter(PortfolioAdapter):
        async def list_accounts(self) -> tuple[BrokerAccount, ...]:
            return (
                BrokerAccount("account-1", "First", "OPEN", "BROKER"),
                BrokerAccount("account-2", "Second", "OPEN", "BROKER"),
            )

    engine, factory = database
    with factory() as session:
        session.add(user_broker_model("broker-2", "account-2"))
        session.commit()
    result = asyncio.run(
        PortfolioSnapshotCollector(
            BrokerRepository(
                [
                    broker("broker-1", "account-1"),
                    broker("broker-2", "account-2"),
                ]
            ),
            lambda _broker: SharedVisibilityAdapter(),
            factory,
            engine,
            clock=lambda: NOW,
        ).collect_once()
    )

    with factory() as session:
        snapshots = PortfolioSnapshotRepository(session).latest_snapshots(result.run_id or "")
    assert result.saved == 2
    assert {(item.user_broker_id, item.account_id) for item in snapshots} == {
        ("broker-1", "account-1"),
        ("broker-2", "account-2"),
    }
