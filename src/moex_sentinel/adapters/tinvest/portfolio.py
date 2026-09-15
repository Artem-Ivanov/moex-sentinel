"""Read-only adapter for T-Invest Sandbox portfolio APIs."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import GetOperationsByCursorRequest

from moex_sentinel.adapters.tinvest.converters import (
    enum_name,
    money_to_domain,
    quotation_to_decimal,
)
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.adapters.tinvest.request_errors import (
    SDK_REQUEST_ERRORS,
    invalid_response_error,
    map_request_error,
)
from moex_sentinel.domain.portfolio import (
    AccountPortfolio,
    BrokerAccount,
    ExternalOperation,
    ExternalPosition,
    Money,
    OperationsPage,
)

SANDBOX_TARGET = "sandbox-invest-public-api.tbank.ru:443"
ClientFactory = Callable[..., Any]


def _default_client_factory(token: str, *, target: str) -> AsyncClient:
    return AsyncClient(token, target=target)


class TInvestPortfolioAdapter:
    def __init__(
        self,
        token: str,
        target: str,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        if target != SANDBOX_TARGET:
            raise ValueError("Only the configured Sandbox target is allowed.")
        self._token = token
        self._target = target
        self._client_factory = client_factory

    async def list_accounts(self) -> tuple[BrokerAccount, ...]:
        """Return sandbox account identities and statuses."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.sandbox.get_sandbox_accounts()
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return tuple(
                BrokerAccount(
                    account_id=account.id,
                    name=account.name,
                    status=enum_name(account.status),
                    account_type=enum_name(account.type),
                )
                for account in response.accounts
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    async def get_portfolio(self, account_id: str) -> AccountPortfolio:
        """Read portfolio and cash balances, summing only the portfolio currency."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                portfolio = await services.sandbox.get_sandbox_portfolio(account_id=account_id)
                positions = await services.sandbox.get_sandbox_positions(account_id=account_id)
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            total = money_to_domain(portfolio.total_amount_portfolio)
            currency = total.currency if total is not None else None
            free_cash = self._sum_money(positions.money, currency) if currency is not None else None
            unrealized = self._quotation_as_money(portfolio.expected_yield, currency)
            return AccountPortfolio(
                account_id=account_id,
                total_amount=total,
                free_cash=free_cash,
                realized_pnl=None,
                unrealized_pnl=unrealized,
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]:
        """Return broker lot quantities and nullable prices without fabricating missing money."""
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.sandbox.get_sandbox_portfolio(account_id=account_id)
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return tuple(
                ExternalPosition(
                    account_id=account_id,
                    instrument_id=position.instrument_uid,
                    ticker=position.ticker,
                    quantity_lots=quotation_to_decimal(position.quantity_lots),
                    average_price=money_to_domain(position.average_position_price),
                    current_price=money_to_domain(position.current_price),
                    expected_yield=self._quotation_as_money(
                        position.expected_yield,
                        getattr(position.average_position_price, "currency", None),
                    ),
                )
                for position in response.positions
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

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
        """Read one cursor page, preserving requested time bounds and source amounts."""
        request = GetOperationsByCursorRequest(
            account_id=account_id,
            instrument_id=instrument_id,
            from_=from_at,
            to=to_at,
            cursor=cursor,
            limit=limit,
        )
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.operations.get_operations_by_cursor(request)
        except SDK_REQUEST_ERRORS as error:
            raise map_request_error(error) from error
        try:
            return OperationsPage(
                items=tuple(
                    ExternalOperation(
                        operation_id=item.id,
                        account_id=item.broker_account_id or account_id,
                        operation_type=enum_name(item.type),
                        state=enum_name(item.state),
                        occurred_at=item.date,
                        payment=money_to_domain(item.payment),
                        price=money_to_domain(item.price),
                        quantity=Decimal(item.quantity),
                        commission=money_to_domain(item.commission),
                        instrument_id=getattr(item, "instrument_uid", None) or None,
                    )
                    for item in response.items
                ),
                next_cursor=response.next_cursor if response.has_next else None,
            )
        except (ValueError, InvalidOperation) as error:
            raise invalid_response_error() from error

    @staticmethod
    def _quotation_as_money(value: Any | None, currency: str | None) -> Money | None:
        """Attach a currency only when both the quotation and its currency exist."""
        if value is None or currency is None:
            return None
        return Money(quotation_to_decimal(value), str(currency).upper())

    @staticmethod
    def _sum_money(values: list[Any], currency: str) -> Money:
        """Sum quotations in the requested currency without converting other currencies."""
        currency = currency.upper()
        matching = [value for value in values if str(value.currency).upper() == currency]
        return Money(sum((quotation_to_decimal(value) for value in matching), Decimal()), currency)


__all__ = ["SANDBOX_TARGET", "TInvestAdapterError", "TInvestPortfolioAdapter"]
