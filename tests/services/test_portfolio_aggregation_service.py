import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker, BrokerField
from moex_sentinel.domain.environment import EnvironmentState
from moex_sentinel.domain.portfolio import (
    AccountPortfolio,
    BrokerAccount,
    ExternalOperation,
    ExternalPosition,
    Money,
    OperationsPage,
)
from moex_sentinel.services.broker_factory import PortfolioAdapterFactory
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.services.portfolio import PortfolioAggregationService


def broker(broker_id: str, name: str, *, enabled: bool = True) -> Broker:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    return Broker(
        id=broker_id,
        display_name=name,
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=enabled,
        fields=(
            BrokerField(name="token", value="synthetic-token"),
            BrokerField(name="fqdn", value="sandbox-invest-public-api.tbank.ru:443"),
        ),
        created_at=now,
        updated_at=now,
    )


class BrokerRepository:
    def __init__(self, records):
        self.records = records

    def list(self):
        return self.records

    def get(self, broker_id: str):
        return next(item for item in self.records if item.id == broker_id)


class PortfolioAdapter:
    def __init__(self, account_id: str, *, failing: bool = False) -> None:
        self.account_id = account_id
        self.failing = failing

    async def list_accounts(self):
        if self.failing:
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Площадка недоступна.", retryable=True)
        return (BrokerAccount(account_id=self.account_id, name="Main", status="OPEN", account_type="BROKER"),)

    async def get_portfolio(self, account_id: str):
        return AccountPortfolio(
            account_id=account_id,
            total_amount=Money(amount=Decimal("100"), currency="RUB"),
            free_cash=Money(amount=Decimal("40"), currency="RUB"),
            realized_pnl=None,
            unrealized_pnl=None,
        )

    async def get_positions(self, account_id: str):
        return (
            ExternalPosition(
                account_id=account_id,
                instrument_id="instrument",
                ticker="TEST",
                quantity_lots=Decimal("1"),
                average_price=None,
                current_price=None,
                expected_yield=None,
            ),
        )

    async def get_operations(
        self,
        account_id: str,
        cursor: str | None,
        limit: int,
        instrument_id: str | None = None,
    ):
        item = ExternalOperation(
            operation_id=f"operation-{account_id}",
            account_id=account_id,
            operation_type="BUY",
            state="EXECUTED",
            occurred_at=datetime(2026, 8, 5, tzinfo=UTC),
            payment=None,
            price=None,
            quantity=Decimal("1"),
            commission=None,
        )
        return OperationsPage((item,), None)


class ProdEnvironment:
    def view(self) -> EnvironmentState:
        return EnvironmentState("PROD", False, False)


def test_factory_reads_required_fields_and_rejects_disabled_broker() -> None:
    created = []
    factory = PortfolioAdapterFactory(lambda token, target: created.append((token, target)) or PortfolioAdapter("a"))

    factory.create(broker("b1", "First"))

    assert created == [("synthetic-token", "sandbox-invest-public-api.tbank.ru:443")]
    with pytest.raises(ValueError, match="отключено") as error:
        factory.create(broker("b2", "Disabled", enabled=False))

    assert "synthetic-token" not in str(error.value)


def test_aggregation_keeps_successful_broker_when_another_fails() -> None:
    records = [broker("b1", "First"), broker("b2", "Second")]
    adapters = {"b1": PortfolioAdapter("a1"), "b2": PortfolioAdapter("a2", failing=True)}
    service = PortfolioAggregationService(BrokerRepository(records), lambda item: adapters[item.id])

    accounts = asyncio.run(service.view_all_accounts())
    positions = asyncio.run(service.view_positions())
    operations = asyncio.run(service.view_operations(50))

    assert accounts.accounts[0].broker_id == "b1"
    assert accounts.total_free_cash == (Money(Decimal("40"), "RUB"),)
    assert accounts.errors[0].broker_id == "b2"
    assert positions.items[0].broker_name == "First"
    assert operations.items[0].account_id == "a1"


def test_operations_resolve_ticker_by_external_instrument_id() -> None:
    class OperationAdapter(PortfolioAdapter):
        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
        ) -> OperationsPage:
            return OperationsPage(
                (
                    ExternalOperation(
                        operation_id="operation-1",
                        account_id=account_id,
                        operation_type="BUY",
                        state="EXECUTED",
                        occurred_at=datetime(2026, 8, 5, tzinfo=UTC),
                        payment=None,
                        price=None,
                        quantity=Decimal("1"),
                        commission=None,
                        instrument_id="external-uid",
                    ),
                ),
                None,
            )

    class Catalog:
        def find_by_external_instrument_id(self, broker_id: str, external_instrument_id: str):
            assert (broker_id, external_instrument_id) == ("b1", "external-uid")
            return type("Instrument", (), {"ticker": "TEST"})()

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]),
        lambda _broker: OperationAdapter("account-1"),
        instruments=Catalog(),
    )

    result = asyncio.run(service.view_operations(50))

    assert result.items[0].operation.ticker == "TEST"


def test_operations_do_not_hide_unexpected_catalog_failure() -> None:
    class OperationAdapter(PortfolioAdapter):
        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
        ) -> OperationsPage:
            return OperationsPage(
                (
                    ExternalOperation(
                        operation_id="operation-1",
                        account_id=account_id,
                        operation_type="BUY",
                        state="EXECUTED",
                        occurred_at=datetime(2026, 8, 5, tzinfo=UTC),
                        payment=None,
                        price=None,
                        quantity=Decimal("1"),
                        commission=None,
                        instrument_id="external-uid",
                    ),
                ),
                None,
            )

    class BrokenCatalog:
        def find_by_external_instrument_id(self, broker_id: str, external_instrument_id: str):
            raise RuntimeError("catalog lookup failed")

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]),
        lambda _broker: OperationAdapter("account-1"),
        instruments=BrokenCatalog(),
    )

    with pytest.raises(RuntimeError, match="catalog lookup failed"):
        asyncio.run(service.view_operations(50))


def test_prod_environment_does_not_read_test_brokers() -> None:
    records = [broker("b1", "First")]
    called = False

    def factory(_broker):
        nonlocal called
        called = True
        return PortfolioAdapter("a1")

    service = PortfolioAggregationService(BrokerRepository(records), factory, ProdEnvironment())

    result = asyncio.run(service.view_all_accounts())

    assert result.accounts == ()
    assert called is False


def test_aggregation_filters_non_instrument_positions_and_cash_movements() -> None:
    class FilteringAdapter(PortfolioAdapter):
        async def get_positions(self, account_id: str):
            return (
                ExternalPosition(
                    account_id=account_id,
                    instrument_id="instrument",
                    ticker="KEEP",
                    quantity_lots=Decimal("1"),
                    average_price=None,
                    current_price=None,
                    expected_yield=None,
                ),
                ExternalPosition(
                    account_id=account_id,
                    instrument_id="",
                    ticker="",
                    quantity_lots=Decimal("1"),
                    average_price=None,
                    current_price=None,
                    expected_yield=None,
                ),
                ExternalPosition(
                    account_id=account_id,
                    instrument_id="instrument-zero",
                    ticker="ZERO",
                    quantity_lots=Decimal("0"),
                    average_price=None,
                    current_price=None,
                    expected_yield=None,
                ),
            )

        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
        ):
            now = datetime(2026, 8, 5, tzinfo=UTC)
            return OperationsPage(
                (
                    ExternalOperation(
                        operation_id="input",
                        account_id=account_id,
                        operation_type="OPERATION_TYPE_INPUT",
                        state="EXECUTED",
                        occurred_at=now,
                        payment=None,
                        price=None,
                        quantity=Decimal(),
                        commission=None,
                    ),
                    ExternalOperation(
                        operation_id="output",
                        account_id=account_id,
                        operation_type="OPERATION_TYPE_OUTPUT_SECURITIES",
                        state="EXECUTED",
                        occurred_at=now,
                        payment=None,
                        price=None,
                        quantity=Decimal(),
                        commission=None,
                    ),
                    ExternalOperation(
                        operation_id="fee",
                        account_id=account_id,
                        operation_type="OPERATION_TYPE_BROKER_FEE",
                        state="EXECUTED",
                        occurred_at=now,
                        payment=None,
                        price=None,
                        quantity=Decimal(),
                        commission=None,
                    ),
                ),
                None,
            )

    records = [broker("b1", "First")]
    service = PortfolioAggregationService(BrokerRepository(records), lambda _: FilteringAdapter("a1"))

    positions = asyncio.run(service.view_positions())
    operations = asyncio.run(service.view_operations(50))

    assert [item.position.ticker for item in positions.items] == ["KEEP"]
    assert [item.operation.operation_id for item in operations.items] == ["fee"]


def test_position_operations_are_scoped_and_only_include_executed_trades() -> None:
    class PositionOperationsAdapter(PortfolioAdapter):
        def __init__(self) -> None:
            super().__init__("account-1")
            self.calls: list[tuple[str, str | None, int, str | None]] = []

        async def get_operations(
            self,
            account_id: str,
            cursor: str | None,
            limit: int,
            instrument_id: str | None = None,
        ) -> OperationsPage:
            self.calls.append((account_id, cursor, limit, instrument_id))
            now = datetime(2026, 8, 5, tzinfo=UTC)
            values = (
                ("buy", "OPERATION_TYPE_BUY", "OPERATION_STATE_EXECUTED"),
                ("sell", "OPERATION_TYPE_SELL", "EXECUTED"),
                ("pending", "OPERATION_TYPE_BUY", "OPERATION_STATE_PROGRESS"),
                ("fee", "OPERATION_TYPE_BROKER_FEE", "OPERATION_STATE_EXECUTED"),
            )
            return OperationsPage(
                tuple(
                    ExternalOperation(
                        operation_id=operation_id,
                        account_id=account_id,
                        operation_type=operation_type,
                        state=state,
                        occurred_at=now,
                        payment=None,
                        price=None,
                        quantity=Decimal("1"),
                        commission=None,
                    )
                    for operation_id, operation_type, state in values
                ),
                None,
            )

    class Catalog:
        def get(self, user_broker_id, instrument_id):
            assert (user_broker_id, instrument_id) == ("b1", "internal-instrument")
            return SimpleNamespace(external_instrument_id="instrument-1")

    adapter = PositionOperationsAdapter()
    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: adapter, instruments=Catalog()
    )

    result = asyncio.run(service.view_position_operations("b1", "account-1", "internal-instrument", 50))

    assert adapter.calls == [("account-1", None, 50, "instrument-1")]
    assert [item.operation.operation_id for item in result.items] == ["buy", "sell"]


@pytest.mark.parametrize(
    "initially_present", [pytest.param(True, id="known-instrument"), pytest.param(False, id="missing-instrument")]
)
def test_operation_ticker_lookup_is_scoped_to_broker_and_one_request(initially_present):
    class RepeatedOperations(PortfolioAdapter):
        async def list_accounts(self):
            return tuple(
                BrokerAccount(account_id=f"account-{index}", name="Main", status="OPEN", account_type="BROKER")
                for index in range(2)
            )

        async def get_operations(self, account_id, cursor, limit, instrument_id=None):
            page = await super().get_operations(account_id, cursor, limit, instrument_id)
            item = page.items[0]
            return OperationsPage(
                tuple(
                    item.model_copy(
                        update={"operation_id": f"{account_id}-{index}", "instrument_id": "shared-external"}
                    )
                    for index in range(2)
                ),
                None,
            )

    class Catalog:
        def __init__(self):
            self.calls = []
            self.generation = 1

        def find_by_external_instrument_id(self, broker_id, external_instrument_id):
            self.calls.append((broker_id, external_instrument_id))
            if self.generation == 1 and not initially_present:
                return None
            return SimpleNamespace(ticker=f"{broker_id}-generation-{self.generation}")

    catalog = Catalog()
    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First"), broker("b2", "Second")]),
        lambda _broker: RepeatedOperations("unused"),
        instruments=catalog,
    )
    first = asyncio.run(service.view_operations(50))

    assert len(first.items) == 8
    assert first.errors == ()
    for item in first.items:
        assert item.operation.ticker == (f"{item.broker_id}-generation-1" if initially_present else None)
    assert sorted(catalog.calls) == [("b1", "shared-external"), ("b2", "shared-external")]

    catalog.generation = 2
    second = asyncio.run(service.view_operations(50))

    assert len(second.items) == 8
    assert second.errors == ()
    assert sorted(catalog.calls) == [("b1", "shared-external")] * 2 + [("b2", "shared-external")] * 2
    for item in second.items:
        assert item.operation.ticker == f"{item.broker_id}-generation-2"


class ScopedPortfolioAdapter(PortfolioAdapter):
    def __init__(self, account_ids):
        super().__init__("unused")
        self.account_ids = account_ids
        self.reads = []

    async def list_accounts(self):
        return tuple(BrokerAccount(account_id, "Main", "OPEN", "BROKER") for account_id in self.account_ids)

    async def get_portfolio(self, account_id):
        self.reads.append(("portfolio", account_id))
        return await super().get_portfolio(account_id)

    async def get_positions(self, account_id):
        self.reads.append(("positions", account_id))
        return await super().get_positions(account_id)

    async def get_operations(self, account_id, cursor, limit, instrument_id=None):
        self.reads.append(("operations", account_id))
        return await super().get_operations(account_id, cursor, limit, instrument_id)


def read_scope(service, kind):
    if kind == "accounts":
        return asyncio.run(service.view_broker_accounts("b1"))
    if kind == "positions":
        return asyncio.run(service.view_positions())
    return asyncio.run(service.view_operations(20))


@pytest.mark.parametrize("kind", ["accounts", "positions", "operations"])
@pytest.mark.parametrize("account_ids", [(), ("foreign",)])
def test_missing_selected_account_reports_error_without_reading_other_accounts(kind, account_ids):
    record = broker("b1", "First").model_copy(update={"account_id": "selected"})
    adapter = ScopedPortfolioAdapter(account_ids)
    service = PortfolioAggregationService(BrokerRepository([record]), lambda _: adapter)

    result = read_scope(service, kind)

    assert len(result.errors) == 1
    assert result.errors[0].code == "BROKER_ACCOUNT_NOT_FOUND"
    assert result.errors[0].message == "Выбранный счёт не найден на площадке."
    assert result.errors[0].account_id == "selected"
    assert adapter.reads == []


@pytest.mark.parametrize("kind", ["accounts", "positions", "operations"])
def test_selected_account_present_reads_only_its_scope(kind):
    record = broker("b1", "First").model_copy(update={"account_id": "selected"})
    adapter = ScopedPortfolioAdapter(("foreign", "selected"))
    service = PortfolioAggregationService(BrokerRepository([record]), lambda _: adapter)

    result = read_scope(service, kind)

    assert result.errors == ()
    assert len(adapter.reads) == 1
    assert adapter.reads[0][1] == "selected"


def test_unbound_account_discovery_retains_all_available_accounts():
    record = broker("b1", "First")
    adapter = ScopedPortfolioAdapter(("first", "second"))
    service = PortfolioAggregationService(BrokerRepository([record]), lambda _: adapter)

    result = asyncio.run(service.view_broker_accounts(record.id))

    assert result.errors == ()
    assert [item.account.account_id for item in result.accounts] == ["first", "second"]


def test_missing_scope_does_not_hide_another_brokers_accounts():
    missing = broker("b1", "First").model_copy(update={"account_id": "selected"})
    good = broker("b2", "Second").model_copy(update={"account_id": "good"})
    adapters = {"b1": ScopedPortfolioAdapter(("foreign",)), "b2": ScopedPortfolioAdapter(("good",))}
    service = PortfolioAggregationService(BrokerRepository([missing, good]), lambda record: adapters[record.id])

    result = asyncio.run(service.view_all_accounts())

    assert [item.broker_id for item in result.accounts] == ["b2"]
    assert [error.code for error in result.errors] == ["BROKER_ACCOUNT_NOT_FOUND"]
    assert adapters["b1"].reads == []


def test_scoped_operations_return_the_complete_filtered_source_page_and_cursor() -> None:
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    source_rows = tuple(
        ExternalOperation(
            operation_id=f"op-{index:03}",
            account_id="account-1",
            operation_type="OPERATION_TYPE_INPUT" if index < 99 else "BUY",
            state="EXECUTED",
            occurred_at=to_at - timedelta(seconds=index),
            payment=None,
            price=None,
            quantity=Decimal("1"),
            commission=None,
        )
        for index in range(500)
    )

    class ScopedAdapter(PortfolioAdapter):
        calls = []

        async def list_accounts(self):
            return (
                BrokerAccount("account-1", "Main", "OPEN", "BROKER"),
                BrokerAccount("other-account", "Other", "OPEN", "BROKER"),
            )

        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            self.calls.append((account_id, cursor, limit, from_at, to_at))
            return OperationsPage(source_rows, "next-page")

    adapter = ScopedAdapter("account-1")
    service = PortfolioAggregationService(BrokerRepository([broker("b1", "First")]), lambda _broker: adapter)

    result = asyncio.run(
        service.view_operations_scoped(
            "b1",
            "account-1",
            limit=500,
            cursor=None,
            from_at=from_at,
            to_at=to_at,
        )
    )

    assert adapter.calls == [("account-1", None, 500, from_at, to_at)]
    assert result.source_item_count == 500
    assert result.selected_item_count == 401
    assert len(result.items) == 401
    assert result.next_cursor == "next-page"
    assert result.source_exhausted is False
    assert result.coverage_complete is False


def test_scoped_operations_do_not_claim_interval_complete_from_a_continuation_page() -> None:
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)

    class ScopedAdapter(PortfolioAdapter):
        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            assert (account_id, cursor, limit, from_at, to_at) == (
                "account-1",
                "prior-page",
                10,
                datetime(2026, 10, 1, tzinfo=UTC),
                datetime(2026, 10, 20, tzinfo=UTC),
            )
            return OperationsPage((), None)

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: ScopedAdapter("account-1")
    )

    result = asyncio.run(
        service.view_operations_scoped(
            "b1",
            "account-1",
            limit=10,
            cursor="prior-page",
            from_at=from_at,
            to_at=to_at,
        )
    )

    assert result.input_cursor == "prior-page"
    assert result.next_cursor is None
    assert result.source_exhausted is True
    assert result.coverage_complete is False


def test_scoped_operations_walked_by_client_cursor_keep_every_selected_row() -> None:
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    pages = {
        None: OperationsPage(
            (
                ExternalOperation(
                    operation_id="ignored",
                    account_id="account-1",
                    operation_type="OPERATION_TYPE_INPUT",
                    state="EXECUTED",
                    occurred_at=to_at,
                    payment=None,
                    price=None,
                    quantity=Decimal("0"),
                    commission=None,
                ),
                ExternalOperation(
                    operation_id="newer",
                    account_id="account-1",
                    operation_type="BUY",
                    state="EXECUTED",
                    occurred_at=to_at - timedelta(seconds=1),
                    payment=None,
                    price=None,
                    quantity=Decimal("1"),
                    commission=None,
                ),
            ),
            "page-2",
        ),
        "page-2": OperationsPage(
            (
                ExternalOperation(
                    operation_id="older",
                    account_id="account-1",
                    operation_type="SELL",
                    state="EXECUTED",
                    occurred_at=to_at - timedelta(seconds=2),
                    payment=None,
                    price=None,
                    quantity=Decimal("1"),
                    commission=None,
                ),
            ),
            None,
        ),
    }

    class CursorAdapter(PortfolioAdapter):
        calls = []

        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            self.calls.append((account_id, cursor, limit, from_at, to_at))
            return pages[cursor]

    adapter = CursorAdapter("account-1")
    service = PortfolioAggregationService(BrokerRepository([broker("b1", "First")]), lambda _broker: adapter)
    cursor = None
    collected = []
    first_coverage = None
    while True:
        page = asyncio.run(
            service.view_operations_scoped("b1", "account-1", limit=2, cursor=cursor, from_at=from_at, to_at=to_at)
        )
        collected.extend(item.operation.operation_id for item in page.items)
        if cursor is None:
            first_coverage = page.coverage_complete
        if page.source_exhausted:
            assert page.coverage_complete is False
            break
        cursor = page.next_cursor

    assert collected == ["newer", "older"]
    assert first_coverage is False
    assert [call[1] for call in adapter.calls] == [None, "page-2"]


@pytest.mark.parametrize(
    ("next_cursor", "rows", "expected_code"),
    [
        ("same", (), "BROKER_OPERATIONS_INVALID_CURSOR"),
        ("x" * 1025, (), "BROKER_OPERATIONS_INVALID_CURSOR"),
        ("next", ("foreign",), "BROKER_OPERATIONS_SCOPE_MISMATCH"),
    ],
)
def test_scoped_operations_reject_bad_source_cursor_and_cross_account_rows(next_cursor, rows, expected_code):
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    operations = tuple(
        ExternalOperation(
            operation_id=f"op-{index}",
            account_id=account_id,
            operation_type="BUY",
            state="EXECUTED",
            occurred_at=to_at - timedelta(seconds=index),
            payment=None,
            price=None,
            quantity=Decimal("1"),
            commission=None,
        )
        for index, account_id in enumerate(rows)
    )

    class InvalidAdapter(PortfolioAdapter):
        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            return OperationsPage(operations, next_cursor)

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: InvalidAdapter("account-1")
    )
    input_cursor = "same" if next_cursor == "same" else None

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=input_cursor, from_at=from_at, to_at=to_at)
    )

    assert result.items == ()
    assert result.errors[0].code == expected_code
    assert result.source_exhausted is None
    assert result.coverage_complete is False
    assert result.errors[0].message == "Не удалось прочитать историю операций."
    assert result.input_cursor == input_cursor


def test_scoped_operations_require_configured_account_before_adapter_creation():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    record = broker("b1", "First").model_copy(update={"account_id": "configured"})
    service = PortfolioAggregationService(
        BrokerRepository([record]),
        lambda _broker: (_ for _ in ()).throw(AssertionError("scope mismatch must fail before SDK creation")),
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "other", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == "BROKER_ACCOUNT_SCOPE_MISMATCH"
    assert result.items == ()
    assert result.source_exhausted is None


def test_scoped_operations_require_account_presence_in_selected_broker():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)

    class ForeignAccountsAdapter(PortfolioAdapter):
        async def list_accounts(self):
            return (BrokerAccount("other", "Other", "OPEN", "BROKER"),)

        async def get_operations(self, *_args, **_kwargs):
            raise AssertionError("account absent from selected broker must not reach operations RPC")

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: ForeignAccountsAdapter("other")
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == "BROKER_ACCOUNT_NOT_FOUND"
    assert result.items == ()
    assert result.source_exhausted is None


def test_scoped_operations_reject_disabled_broker_before_adapter_creation():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First", enabled=False)]),
        lambda _broker: (_ for _ in ()).throw(AssertionError("disabled scope must not create an adapter")),
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == "BROKER_DISABLED"
    assert result.items == ()
    assert result.source_exhausted is None
    assert result.coverage_complete is False


def test_scoped_operations_mark_naive_source_timestamp_incomplete_and_sanitize_error():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    operation = ExternalOperation(
        operation_id="naive-time",
        account_id="account-1",
        operation_type="BUY",
        state="EXECUTED",
        occurred_at=datetime(2026, 10, 19, 12),
        payment=None,
        price=None,
        quantity=Decimal("1"),
        commission=None,
    )

    class NaiveTimestampAdapter(PortfolioAdapter):
        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            return OperationsPage((operation,), None)

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: NaiveTimestampAdapter("account-1")
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == "BROKER_OPERATIONS_INVALID_TIMESTAMP"
    assert result.errors[0].message == "Не удалось прочитать историю операций."
    assert result.items == ()
    assert result.source_exhausted is None
    assert result.coverage_complete is False


def test_scoped_operations_filter_millisecond_boundaries_deduplicate_and_sort_stably():
    from_at = datetime(2026, 10, 1, 0, 0, 0, 1000, tzinfo=UTC)
    to_at = datetime(2026, 10, 1, 0, 0, 0, 4000, tzinfo=UTC)

    def operation(operation_id, occurred_at):
        return ExternalOperation(
            operation_id=operation_id,
            account_id="account-1",
            operation_type="BUY",
            state="EXECUTED",
            occurred_at=occurred_at,
            payment=None,
            price=None,
            quantity=Decimal("1"),
            commission=None,
        )

    at_lower = operation("lower", from_at + timedelta(microseconds=999))
    at_upper = operation("upper", to_at + timedelta(microseconds=999))
    after_upper = operation("after-upper", to_at + timedelta(milliseconds=1))
    same_time_z = operation("z", to_at - timedelta(microseconds=1000))
    same_time_a = operation("a", to_at - timedelta(microseconds=1000))
    page = OperationsPage((at_lower, same_time_z, at_upper, after_upper, same_time_a, same_time_a), None)

    class BoundaryAdapter(PortfolioAdapter):
        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
            return page

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: BoundaryAdapter("account-1")
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert [item.operation.operation_id for item in result.items] == ["upper", "a", "z"]
    assert result.source_item_count == 6
    assert result.selected_item_count == 3
    assert result.source_exhausted is True
    assert result.coverage_complete is True


@pytest.mark.parametrize("invalid_page", ["oversized", "conflicting_duplicate"])
def test_scoped_operations_reject_untrustworthy_source_pages(invalid_page):
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    operation = ExternalOperation(
        operation_id="same",
        account_id="account-1",
        operation_type="BUY",
        state="EXECUTED",
        occurred_at=to_at - timedelta(seconds=1),
        payment=None,
        price=None,
        quantity=Decimal("1"),
        commission=None,
    )
    if invalid_page == "oversized":
        items = (operation, operation.model_copy(update={"operation_id": "other"}))
        limit = 1
        expected_code = "BROKER_OPERATIONS_INVALID_PAGE"
    else:
        items = (operation, operation.model_copy(update={"quantity": Decimal("2")}))
        limit = 2
        expected_code = "BROKER_OPERATIONS_CONFLICTING_DUPLICATE"

    class InvalidAdapter(PortfolioAdapter):
        async def get_operations(
            self, account_id, cursor, requested_limit, instrument_id=None, *, from_at=None, to_at=None
        ):
            return OperationsPage(items, None)

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]), lambda _broker: InvalidAdapter("account-1")
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=limit, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == expected_code
    assert result.items == ()
    assert result.source_exhausted is None
    assert result.coverage_complete is False


def test_scoped_operations_sanitize_adapter_error_and_do_not_read_other_scopes():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)

    class FailedAdapter(PortfolioAdapter):
        async def list_accounts(self):
            raise TInvestAdapterError("BROKER_FAILURE", "secret-token leaked", retryable=False)

    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First"), broker("b2", "Second")]),
        lambda selected: FailedAdapter("account-1"),
    )

    result = asyncio.run(
        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
    )

    assert result.errors[0].code == "BROKER_OPERATIONS_UNAVAILABLE"
    assert "secret-token" not in result.errors[0].message
    assert result.source_exhausted is None
    assert result.coverage_complete is False


def test_scoped_operations_reject_wrong_environment_before_adapter_creation():
    from_at = datetime(2026, 10, 1, tzinfo=UTC)
    to_at = datetime(2026, 10, 20, tzinfo=UTC)
    service = PortfolioAggregationService(
        BrokerRepository([broker("b1", "First")]),
        lambda _broker: (_ for _ in ()).throw(AssertionError("wrong environment must fail before SDK")),
        environment=ProdEnvironment(),
    )

    with pytest.raises(EnvironmentMismatchError, match="inactive environment"):
        asyncio.run(
            service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
        )
