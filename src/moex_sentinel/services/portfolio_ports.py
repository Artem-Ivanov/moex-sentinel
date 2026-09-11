"""Read-only broker portfolio boundary."""

from datetime import datetime
from typing import Protocol

from moex_sentinel.domain.portfolio import (
    AccountPortfolio,
    ActiveBrokerOrder,
    BrokerAccount,
    ExternalPosition,
    OperationsPage,
)


class PortfolioPort(Protocol):
    async def list_accounts(self) -> tuple[BrokerAccount, ...]: ...

    async def get_portfolio(self, account_id: str) -> AccountPortfolio: ...

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]: ...

    async def get_operations(
        self,
        account_id: str,
        cursor: str | None,
        limit: int,
        instrument_id: str | None = None,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> OperationsPage: ...


class PositionAdoptionBrokerPort(Protocol):
    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]: ...

    async def list_active_orders(
        self,
        account_id: str,
        instrument_id: str,
    ) -> tuple[ActiveBrokerOrder, ...]: ...
