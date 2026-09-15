"""SDK mapping for Sandbox limit-order execution."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from moex_sentinel.adapters.tinvest.order_execution import TInvestOrderExecutionAdapter
from sentinel_contracts.broker_execution import OrderSide


def quotation(value: str) -> SimpleNamespace:
    amount = Decimal(value)
    units = int(amount)
    nano = int((amount - units) * Decimal("1000000000"))
    return SimpleNamespace(units=units, nano=nano)


def money(value: str, currency: str = "rub") -> SimpleNamespace:
    return SimpleNamespace(**quotation(value).__dict__, currency=currency)


class MarketData:
    async def get_order_book(self, **request):
        assert request == {"instrument_id": "instrument-1", "depth": 20}
        return SimpleNamespace(
            bids=[SimpleNamespace(price=quotation("99.90"), quantity=4)],
            asks=[SimpleNamespace(price=quotation("100.10"), quantity=3)],
            orderbook_ts=datetime(2026, 8, 5, 12, tzinfo=UTC),
        )

    async def get_trading_status(self, **request):
        assert request == {"instrument_id": "instrument-1"}
        return SimpleNamespace(
            trading_status=SimpleNamespace(name="SECURITY_TRADING_STATUS_NORMAL_TRADING"),
            limit_order_available_flag=True,
            market_order_available_flag=True,
            api_trade_available_flag=True,
        )


class Orders:
    def __init__(self) -> None:
        self.posted = None

    async def get_order_price(self, request):
        assert request.account_id == "account-1"
        assert request.quantity == 2
        return SimpleNamespace(
            total_order_amount=money("200.20"),
            initial_order_amount=money("200.20"),
            executed_commission=money("0.60"),
            service_commission=money("0"),
            deal_commission=money("0.60"),
        )

    async def post_order(self, **request):
        self.posted = request
        return SimpleNamespace(
            order_id="broker-order-1",
            order_request_id=request["order_id"],
            execution_report_status=SimpleNamespace(name="EXECUTION_REPORT_STATUS_NEW"),
            lots_requested=2,
            lots_executed=0,
            initial_order_price=money("200.20"),
            executed_order_price=money("0"),
            total_order_amount=money("0"),
            initial_commission=money("0.60"),
            executed_commission=money("0"),
        )

    async def get_order_state(self, **request):
        return SimpleNamespace(
            order_id=request["order_id"],
            order_request_id="00000000-0000-4000-8000-000000000001",
            execution_report_status=SimpleNamespace(name="EXECUTION_REPORT_STATUS_PARTIALLYFILL"),
            lots_requested=2,
            lots_executed=1,
            initial_order_price=money("200.20"),
            executed_order_price=money("100.10"),
            average_position_price=money("100.10"),
            total_order_amount=money("100.10"),
            initial_commission=money("0.60"),
            executed_commission=money("0.30"),
            order_date=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
        )

    async def cancel_order(self, **request):
        return SimpleNamespace(time=datetime(2026, 8, 5, 12, 1, tzinfo=UTC))

    async def get_orders(self, **request):
        assert request == {"account_id": "account-1"}
        return SimpleNamespace(
            orders=[
                SimpleNamespace(
                    order_id="broker-order-1",
                    order_request_id="00000000-0000-4000-8000-000000000001",
                    instrument_uid="instrument-1",
                    execution_report_status=SimpleNamespace(name="EXECUTION_REPORT_STATUS_NEW"),
                    lots_requested=2,
                    lots_executed=0,
                    initial_order_price=money("200.20"),
                    executed_order_price=money("0"),
                    total_order_amount=money("0"),
                    initial_commission=money("0.60"),
                    executed_commission=money("0"),
                )
            ]
        )


class Services:
    def __init__(self) -> None:
        self.market_data = MarketData()
        self.orders = Orders()
        self.sandbox = SimpleNamespace(get_sandbox_portfolio=self.get_sandbox_portfolio)

    async def get_sandbox_portfolio(self, *, account_id: str):
        assert account_id == "account-1"
        return SimpleNamespace(
            positions=[
                SimpleNamespace(
                    instrument_uid="instrument-1",
                    quantity_lots=quotation("2"),
                    average_position_price=money("100"),
                    current_price=money("102"),
                )
            ]
        )


class ClientContext:
    def __init__(self, services: Services) -> None:
        self.services = services

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *_args):
        return None


@pytest.fixture
def execution_adapter() -> tuple[Services, TInvestOrderExecutionAdapter]:
    services = Services()
    adapter = TInvestOrderExecutionAdapter(
        "synthetic-token",
        "sandbox-invest-public-api.tbank.ru:443",
        client_factory=lambda *_args, **_kwargs: ClientContext(services),
    )
    return services, adapter


def test_maps_order_book(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    book = asyncio.run(adapter.get_order_book("instrument-1", depth=20))

    assert book.best_bid.price == Decimal("99.9")
    assert book.best_ask.price == Decimal("100.1")


def test_estimates_limit_order(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    estimate = asyncio.run(
        adapter.estimate_limit_order("account-1", "instrument-1", OrderSide.BUY, 2, Decimal("100.10"))
    )

    assert estimate.total_amount == Decimal("200.2")
    assert estimate.estimated_commission == Decimal("0.6")


def test_submits_limit_order(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    services, adapter = execution_adapter
    submitted = asyncio.run(
        adapter.submit_limit_order(
            "account-1", "instrument-1", OrderSide.BUY, 2, Decimal("100.10"), "00000000-0000-4000-8000-000000000001"
        )
    )

    assert submitted.broker_order_id == "broker-order-1"
    assert services.orders.posted["order_id"] == "00000000-0000-4000-8000-000000000001"


def test_maps_order_state(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    state = asyncio.run(adapter.get_order_state("account-1", "broker-order-1"))

    assert state.status == "PARTIALLY_FILLED"
    assert state.executed_lots == 1
    assert state.executed_commission == Decimal("0.3")
    assert state.executed_price == Decimal("100.1")
    assert state.executed_at == datetime(2026, 8, 5, 12, 2, tzinfo=UTC)


def test_finds_order_by_idempotency_key(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    reconciled = asyncio.run(adapter.find_by_idempotency_key("account-1", "00000000-0000-4000-8000-000000000001"))

    assert reconciled is not None
    assert reconciled.broker_order_id == "broker-order-1"


def test_lists_active_orders(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    active_orders = asyncio.run(adapter.list_active_orders("account-1", "instrument-1"))

    assert active_orders[0].broker_order_id == "broker-order-1"
    assert active_orders[0].status == "EXECUTION_REPORT_STATUS_NEW"


def test_cancels_order(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    cancelled_at = asyncio.run(adapter.cancel_order("account-1", "broker-order-1"))

    assert cancelled_at == datetime(2026, 8, 5, 12, 1, tzinfo=UTC)


def test_maps_position(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    position = asyncio.run(adapter.get_position("account-1", "instrument-1"))

    assert position is not None
    assert position.quantity_lots == Decimal("2")
    assert position.current_price == Decimal("102")


def test_maps_trading_status(execution_adapter: tuple[Services, TInvestOrderExecutionAdapter]) -> None:
    _services, adapter = execution_adapter
    trading_status = asyncio.run(adapter.get_trading_status("instrument-1"))

    assert trading_status.limit_order_available is True
    assert trading_status.api_trade_available is True


def test_rejects_non_sandbox_target_before_network_io() -> None:
    with pytest.raises(ValueError, match="Sandbox"):
        TInvestOrderExecutionAdapter("synthetic-token", "invest-public-api.tbank.ru:443")
