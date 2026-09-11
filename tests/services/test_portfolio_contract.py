"""SDK-neutral portfolio contract tests."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from moex_sentinel.domain.portfolio import (
    AccountPortfolio,
    BrokerAccount,
    ExternalOperation,
    ExternalPosition,
    Money,
    OperationsPage,
)
from moex_sentinel.services.portfolio_ports import PortfolioPort


class FakePortfolioAdapter:
    async def list_accounts(self) -> tuple[BrokerAccount, ...]:
        return (BrokerAccount("account-1", "Sandbox", "OPEN", "BROKER"),)

    async def get_portfolio(self, account_id: str) -> AccountPortfolio:
        return AccountPortfolio(account_id, Money(Decimal("100.50"), "RUB"), None, None, None)

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]:
        return (ExternalPosition(account_id, "instrument-1", "TEST", Decimal("2"), None, None, None),)

    async def get_operations(self, account_id: str, cursor: str | None, limit: int) -> OperationsPage:
        operation = ExternalOperation(
            "operation-1",
            account_id,
            "BUY",
            "EXECUTED",
            datetime(2026, 8, 5, tzinfo=UTC),
            Money(Decimal("-100.50"), "RUB"),
            Money(Decimal("50.25"), "RUB"),
            Decimal("2"),
            None,
        )
        return OperationsPage((operation,), cursor)


def test_portfolio_port_preserves_decimal_and_nullable_sandbox_values() -> None:
    adapter: PortfolioPort = FakePortfolioAdapter()

    portfolio = asyncio.run(adapter.get_portfolio("account-1"))
    positions = asyncio.run(adapter.get_positions("account-1"))

    assert portfolio.total_amount == Money(Decimal("100.50"), "RUB")
    assert portfolio.free_cash is None
    assert positions[0].average_price is None


def test_operations_keep_external_identity_and_cursor() -> None:
    adapter: PortfolioPort = FakePortfolioAdapter()

    page = asyncio.run(adapter.get_operations("account-1", "next-page", 50))

    assert page.items[0].operation_id == "operation-1"
    assert page.next_cursor == "next-page"
