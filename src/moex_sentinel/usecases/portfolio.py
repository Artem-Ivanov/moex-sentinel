"""User intents for viewing read-only broker data."""

from typing import Protocol

from moex_sentinel.domain.portfolio import (
    BrokerAccountsView,
    BrokerOperationsView,
    BrokerPositionsView,
)
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.usecases.errors import UseCaseError


class PortfolioServicePort(Protocol):
    async def view_broker_accounts(self, broker_id: str) -> BrokerAccountsView: ...

    async def view_all_accounts(self) -> BrokerAccountsView: ...

    async def view_positions(self) -> BrokerPositionsView: ...

    async def view_operations(self, limit: int) -> BrokerOperationsView: ...


class ViewBrokerAccountsUsecase:
    def __init__(self, service: PortfolioServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str) -> BrokerAccountsView:
        try:
            return await self._service.view_broker_accounts(broker_id)
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error


class ViewPortfolioSummaryUsecase:
    def __init__(self, service: PortfolioServicePort) -> None:
        self._service = service

    async def execute(self) -> BrokerAccountsView:
        return await self._service.view_all_accounts()


class ViewOpenPositionsUsecase:
    def __init__(self, service: PortfolioServicePort) -> None:
        self._service = service

    async def execute(self) -> BrokerPositionsView:
        return await self._service.view_positions()


class ViewRecentOperationsUsecase:
    def __init__(self, service: PortfolioServicePort) -> None:
        self._service = service

    async def execute(self, limit: int = 20) -> BrokerOperationsView:
        if not 1 <= limit <= 500:
            raise UseCaseError("INVALID_OPERATIONS_LIMIT", "Лимит должен быть от 1 до 500.")
        return await self._service.view_operations(limit)
