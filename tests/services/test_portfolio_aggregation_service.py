import asyncio
from datetime import UTC, datetime
from decimal import Decimal

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

    adapter = PositionOperationsAdapter()
    service = PortfolioAggregationService(BrokerRepository([broker("b1", "First")]), lambda _broker: adapter)

    result = asyncio.run(service.view_position_operations("b1", "account-1", "instrument-1", 50))

    assert adapter.calls == [("account-1", None, 50, "instrument-1")]
    assert [item.operation.operation_id for item in result.items] == ["buy", "sell"]
