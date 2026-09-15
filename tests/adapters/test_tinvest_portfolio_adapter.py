"""Offline tests for the T-Invest read-only adapter."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from grpc import StatusCode
from grpc.aio import AioRpcError

from moex_sentinel.adapters.tinvest.portfolio import (
    SANDBOX_TARGET,
    TInvestAdapterError,
    TInvestPortfolioAdapter,
)
from moex_sentinel.domain.portfolio import Money


def value(units: int, nano: int = 0, currency: str = "RUB") -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano, currency=currency)


class FakeSandboxService:
    async def get_sandbox_accounts(self):
        return SimpleNamespace(accounts=[SimpleNamespace(id="account-1", name="Main", status="OPEN", type="BROKER")])

    async def get_sandbox_portfolio(self, *, account_id: str):
        assert account_id == "account-1"
        return SimpleNamespace(
            account_id=account_id,
            total_amount_portfolio=value(100, 500_000_000),
            expected_yield=value(2, 250_000_000),
            positions=[
                SimpleNamespace(
                    instrument_uid="instrument-1",
                    ticker="TEST",
                    quantity=value(99),
                    quantity_lots=value(2),
                    average_position_price=value(50, 250_000_000),
                    current_price=value(51),
                    expected_yield=value(1, 500_000_000),
                )
            ],
        )

    async def get_sandbox_positions(self, *, account_id: str):
        return SimpleNamespace(account_id=account_id, money=[value(40, 250_000_000)])


class FakeOperationsService:
    def __init__(self) -> None:
        self.request = None

    async def get_operations_by_cursor(self, request):
        self.request = request
        return SimpleNamespace(
            has_next=True,
            next_cursor="next",
            items=[
                SimpleNamespace(
                    id="operation-1",
                    broker_account_id="account-1",
                    type="BUY",
                    state="EXECUTED",
                    date=datetime(2026, 8, 5, tzinfo=UTC),
                    payment=value(-50, -500_000_000),
                    price=value(50, 250_000_000),
                    quantity=1,
                    commission=None,
                    instrument_uid="instrument-1",
                )
            ],
        )


class FakeContext:
    def __init__(self, services: object) -> None:
        self.services = services

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *_args):
        return None


def adapter_with(services: object) -> TInvestPortfolioAdapter:
    return TInvestPortfolioAdapter(
        token="synthetic-token",  # noqa: S106 - explicit synthetic test credential
        target=SANDBOX_TARGET,
        client_factory=lambda *_args, **_kwargs: FakeContext(services),
    )


@pytest.fixture
def portfolio_adapter() -> tuple[TInvestPortfolioAdapter, FakeOperationsService]:
    operations = FakeOperationsService()
    services = SimpleNamespace(sandbox=FakeSandboxService(), operations=operations)
    return adapter_with(services), operations


def test_adapter_maps_accounts(portfolio_adapter: tuple[TInvestPortfolioAdapter, FakeOperationsService]) -> None:
    adapter, _operations = portfolio_adapter
    accounts = asyncio.run(adapter.list_accounts())

    assert accounts[0].account_id == "account-1"


def test_adapter_maps_portfolio(portfolio_adapter: tuple[TInvestPortfolioAdapter, FakeOperationsService]) -> None:
    adapter, _operations = portfolio_adapter
    portfolio = asyncio.run(adapter.get_portfolio("account-1"))

    assert portfolio.total_amount == Money(Decimal("100.5"), "RUB")
    assert portfolio.free_cash == Money(Decimal("40.25"), "RUB")
    assert portfolio.realized_pnl is None


def test_adapter_maps_positions(portfolio_adapter: tuple[TInvestPortfolioAdapter, FakeOperationsService]) -> None:
    adapter, _operations = portfolio_adapter
    positions = asyncio.run(adapter.get_positions("account-1"))

    assert positions[0].quantity_lots == Decimal("2")
    assert positions[0].average_price == Money(Decimal("50.25"), "RUB")


def test_adapter_maps_operations(portfolio_adapter: tuple[TInvestPortfolioAdapter, FakeOperationsService]) -> None:
    adapter, operations = portfolio_adapter
    from_at = datetime(2026, 8, 4, tzinfo=UTC)
    to_at = datetime(2026, 8, 5, tzinfo=UTC)
    page = asyncio.run(adapter.get_operations("account-1", "cursor", 50, from_at=from_at, to_at=to_at))

    assert page.items[0].payment == Money(Decimal("-50.5"), "RUB")
    assert page.items[0].instrument_id == "instrument-1"
    assert page.next_cursor == "next"
    assert operations.request.account_id == "account-1"
    assert operations.request.cursor == "cursor"
    assert operations.request.limit == 50
    assert operations.request.from_ == from_at
    assert operations.request.to == to_at


def test_adapter_rejects_non_sandbox_target_before_client_creation() -> None:
    called = False

    def factory(*_args, **_kwargs):
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="Sandbox target"):
        TInvestPortfolioAdapter("synthetic-token", "invest-public-api.tbank.ru:443", factory)

    assert called is False


def test_portfolio_free_cash_uses_the_portfolio_valuation_currency() -> None:
    class MixedCashSandbox(FakeSandboxService):
        async def get_sandbox_positions(self, *, account_id: str):
            return SimpleNamespace(
                account_id=account_id,
                money=[
                    value(10, currency="USD"),
                    value(40, currency="RUB"),
                    value(2, 500_000_000, currency="RUB"),
                ],
            )

    adapter = adapter_with(SimpleNamespace(sandbox=MixedCashSandbox()))

    portfolio = asyncio.run(adapter.get_portfolio("account-1"))

    assert portfolio.free_cash == Money(Decimal("42.5"), "RUB")


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (StatusCode.UNAUTHENTICATED, "BROKER_AUTH_FAILED", False),
        (StatusCode.PERMISSION_DENIED, "BROKER_FORBIDDEN", False),
        (StatusCode.RESOURCE_EXHAUSTED, "BROKER_RATE_LIMITED", True),
        (StatusCode.UNAVAILABLE, "BROKER_UNAVAILABLE", True),
        (StatusCode.DEADLINE_EXCEEDED, "BROKER_UNAVAILABLE", True),
    ],
)
def test_adapter_maps_transport_errors_safely(status, code: str, retryable: bool) -> None:
    class FailingSandbox:
        async def get_sandbox_accounts(self):
            raise AioRpcError(status, details="transport detail synthetic-token")

    adapter = adapter_with(SimpleNamespace(sandbox=FailingSandbox()))

    with pytest.raises(TInvestAdapterError) as error:
        asyncio.run(adapter.list_accounts())

    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "transport detail" not in str(error.value)
    assert "synthetic-token" not in str(error.value)
