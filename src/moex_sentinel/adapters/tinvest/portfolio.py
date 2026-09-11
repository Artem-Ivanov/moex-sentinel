"""Read-only adapter for T-Invest Sandbox portfolio APIs."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import GetOperationsByCursorRequest

from moex_sentinel.adapters.tinvest.converters import (
    enum_name,
    money_to_domain,
    quotation_to_decimal,
)
from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
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
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.sandbox.get_sandbox_accounts()
            return tuple(
                BrokerAccount(
                    account_id=account.id,
                    name=account.name,
                    status=enum_name(account.status),
                    account_type=enum_name(account.type),
                )
                for account in response.accounts
            )
        except Exception as error:
            raise self._map_error(error) from error

    async def get_portfolio(self, account_id: str) -> AccountPortfolio:
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                portfolio = await services.sandbox.get_sandbox_portfolio(account_id=account_id)
                positions = await services.sandbox.get_sandbox_positions(account_id=account_id)
            total = money_to_domain(portfolio.total_amount_portfolio)
            currency = total.currency if total is not None else None
            free_cash = self._sum_money(positions.money, currency) if currency is not None else None
            unrealized = self._quotation_as_money(portfolio.expected_yield, currency)
            return AccountPortfolio(account_id, total, free_cash, None, unrealized)
        except Exception as error:
            raise self._map_error(error) from error

    async def get_positions(self, account_id: str) -> tuple[ExternalPosition, ...]:
        try:
            async with self._client_factory(self._token, target=self._target) as services:
                response = await services.sandbox.get_sandbox_portfolio(account_id=account_id)
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
        except Exception as error:
            raise self._map_error(error) from error

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
        except Exception as error:
            raise self._map_error(error) from error

    @staticmethod
    def _quotation_as_money(value: Any | None, currency: str | None) -> Money | None:
        if value is None or currency is None:
            return None
        return Money(quotation_to_decimal(value), str(currency).upper())

    @staticmethod
    def _sum_money(values: list[Any], currency: str) -> Money:
        currency = currency.upper()
        matching = [value for value in values if str(value.currency).upper() == currency]
        return Money(sum((quotation_to_decimal(value) for value in matching), Decimal()), currency)

    @staticmethod
    def _map_error(error: Exception) -> TInvestAdapterError:
        code_method = getattr(error, "code", None)
        status = enum_name(code_method()) if callable(code_method) else ""
        mapping = {
            "UNAUTHENTICATED": (
                "BROKER_AUTH_FAILED",
                "Проверка токена не пройдена.",
                False,
            ),
            "PERMISSION_DENIED": ("BROKER_FORBIDDEN", "Недостаточно прав доступа.", False),
            "RESOURCE_EXHAUSTED": (
                "BROKER_RATE_LIMITED",
                "Превышен лимит запросов.",
                True,
            ),
            "UNAVAILABLE": ("BROKER_UNAVAILABLE", "Площадка временно недоступна.", True),
            "DEADLINE_EXCEEDED": (
                "BROKER_UNAVAILABLE",
                "Площадка временно недоступна.",
                True,
            ),
        }
        code, message, retryable = mapping.get(
            status,
            ("BROKER_UNAVAILABLE", "Не удалось получить данные площадки.", False),  # noqa: RUF001
        )
        return TInvestAdapterError(code, message, retryable=retryable)


__all__ = ["SANDBOX_TARGET", "TInvestAdapterError", "TInvestPortfolioAdapter"]
