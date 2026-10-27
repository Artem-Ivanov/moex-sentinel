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
from moex_sentinel.domain.execution_commissions import BrokerFeeScanClaim
from moex_sentinel.domain.portfolio import Money
from moex_sentinel.services.execution_commissions import ExecutionCommissionService


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
                    parent_operation_id="exchange-order-1",
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
    assert page.items[0].parent_operation_id == "exchange-order-1"
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

    with pytest.raises(ValueError, match="target"):
        TInvestPortfolioAdapter("synthetic-token", "foreign-public-api.tbank.ru:443", factory)

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


def test_actual_fee_enum_survives_adapter_and_commission_service() -> None:
    class Operations:
        async def get_operations_by_cursor(self, _request):
            return SimpleNamespace(
                has_next=False,
                next_cursor=None,
                items=[
                    SimpleNamespace(
                        id="fee-op",
                        broker_account_id="account-1",
                        type="OPERATION_TYPE_BROKER_FEE",
                        state="OPERATION_STATE_EXECUTED",
                        date=datetime(2026, 10, 27, tzinfo=UTC),
                        payment=value(-1, -720_000_000),
                        price=None,
                        quantity=0,
                        commission=None,
                        instrument_uid="instrument-1",
                        parent_operation_id="exchange-order-1",
                    )
                ],
            )

    class Repository:
        saved = None

        def apply_page(self, _claim, observations, _cursor, _now):
            self.saved = observations
            return True

        def fail_page(self, _claim, _error):
            raise AssertionError("Valid fee source must not be rejected")

    adapter = adapter_with(SimpleNamespace(operations=Operations()))
    repository = Repository()
    service = ExecutionCommissionService(repository, now=lambda: datetime(2026, 10, 28, tzinfo=UTC))
    claim = BrokerFeeScanClaim(
        user_broker_id="broker-1",
        account_id="account-1",
        scan_pass_id="scan-abc",  # noqa: S106 - synthetic test identity, not a credential
        claim_id="claim-abc",
        lower_bound=datetime(2026, 10, 27, tzinfo=UTC),
        upper_bound=datetime(2026, 10, 28, tzinfo=UTC),
        cursor=None,
    )
    page = asyncio.run(
        adapter.get_operations(
            "account-1",
            None,
            100,
            from_at=claim.lower_bound,
            to_at=claim.upper_bound,
        )
    )

    assert service.accept_page(claim, page) is True
    assert repository.saved is not None
    assert repository.saved[0].amount == Decimal("1.72")


def test_missing_source_account_on_fee_is_not_replaced_by_requested_account() -> None:
    class Operations:
        async def get_operations_by_cursor(self, _request):
            item = SimpleNamespace(
                id="fee-op",
                broker_account_id=None,
                type="OPERATION_TYPE_BROKER_FEE",
                state="OPERATION_STATE_EXECUTED",
                date=datetime(2026, 10, 27, tzinfo=UTC),
                payment=value(-1, -720_000_000),
                price=None,
                quantity=0,
                commission=None,
                instrument_uid="instrument-1",
                parent_operation_id="exchange-order-1",
            )
            return SimpleNamespace(has_next=False, next_cursor=None, items=[item])

    adapter = adapter_with(SimpleNamespace(operations=Operations()))

    page = asyncio.run(adapter.get_operations("account-1", None, 100))

    assert page.items[0].account_id == ""

    class Repository:
        error = None

        def fail_page(self, _claim, error):
            self.error = error
            return True

        def apply_page(self, *_args):
            raise AssertionError("Missing source account must fail closed")

    repository = Repository()
    service = ExecutionCommissionService(repository)
    claim = BrokerFeeScanClaim(
        user_broker_id="broker-1",
        account_id="account-1",
        scan_pass_id="scan-account",  # noqa: S106 - synthetic test identity, not a credential
        claim_id="claim-account",
        lower_bound=datetime(2026, 10, 27, tzinfo=UTC),
        upper_bound=datetime(2026, 10, 28, tzinfo=UTC),
        cursor=None,
    )

    assert service.accept_page(claim, page) is False
    assert repository.error == "SOURCE_ACCOUNT_MISMATCH"
