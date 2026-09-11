"""Public schemas for SDK-neutral portfolio data."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from moex_sentinel.domain.portfolio import (
    BrokerAccountsView,
    BrokerOperationsView,
    BrokerPositionsView,
    Money,
)
from sentinel_contracts.base import PositionalModel


class StrictSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")


class MoneySchema(StrictSchema):
    amount: Decimal
    currency: str

    @classmethod
    def from_domain(cls, value: Money | None) -> "MoneySchema | None":
        return None if value is None else cls(amount=value.amount, currency=value.currency)

    @classmethod
    def from_money(cls, value: Money) -> "MoneySchema":
        return cls(amount=value.amount, currency=value.currency)


class ReadErrorSchema(StrictSchema):
    broker_id: str
    broker_name: str
    account_id: str | None
    code: str
    message: str


class AccountSchema(StrictSchema):
    broker_id: str
    broker_name: str
    account_id: str
    name: str
    status: str
    account_type: str
    total_amount: MoneySchema | None
    free_cash: MoneySchema | None
    realized_pnl: MoneySchema | None
    unrealized_pnl: MoneySchema | None


class AccountsResponseSchema(StrictSchema):
    accounts: list[AccountSchema]
    total_amounts: list[MoneySchema]
    total_free_cash: list[MoneySchema]
    errors: list[ReadErrorSchema]

    @classmethod
    def from_domain(cls, value: BrokerAccountsView) -> "AccountsResponseSchema":
        return cls(
            accounts=[
                AccountSchema(
                    broker_id=item.broker_id,
                    broker_name=item.broker_name,
                    account_id=item.account.account_id,
                    name=item.account.name,
                    status=item.account.status,
                    account_type=item.account.account_type,
                    total_amount=MoneySchema.from_domain(item.portfolio.total_amount),
                    free_cash=MoneySchema.from_domain(item.portfolio.free_cash),
                    realized_pnl=MoneySchema.from_domain(item.portfolio.realized_pnl),
                    unrealized_pnl=MoneySchema.from_domain(item.portfolio.unrealized_pnl),
                )
                for item in value.accounts
            ],
            total_amounts=[MoneySchema.from_money(item) for item in value.total_amounts],
            total_free_cash=[MoneySchema.from_money(item) for item in value.total_free_cash],
            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
        )


class PositionSchema(StrictSchema):
    broker_id: str
    broker_name: str
    account_id: str
    instrument_id: str
    ticker: str
    quantity: Decimal
    average_price: MoneySchema | None
    current_price: MoneySchema | None
    expected_yield: MoneySchema | None


class PositionsResponseSchema(StrictSchema):
    items: list[PositionSchema]
    errors: list[ReadErrorSchema]

    @classmethod
    def from_domain(cls, value: BrokerPositionsView) -> "PositionsResponseSchema":
        return cls(
            items=[
                PositionSchema(
                    broker_id=item.broker_id,
                    broker_name=item.broker_name,
                    account_id=item.position.account_id,
                    instrument_id=item.position.instrument_id,
                    ticker=item.position.ticker,
                    quantity=item.position.quantity_lots,
                    average_price=MoneySchema.from_domain(item.position.average_price),
                    current_price=MoneySchema.from_domain(item.position.current_price),
                    expected_yield=MoneySchema.from_domain(item.position.expected_yield),
                )
                for item in value.items
            ],
            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
        )


class OperationSchema(StrictSchema):
    broker_id: str
    broker_name: str
    operation_id: str
    account_id: str
    operation_type: str
    state: str
    occurred_at: datetime
    payment: MoneySchema | None
    price: MoneySchema | None
    quantity: Decimal
    commission: MoneySchema | None
    instrument_id: str | None
    ticker: str | None


class OperationsResponseSchema(StrictSchema):
    items: list[OperationSchema]
    errors: list[ReadErrorSchema]

    @classmethod
    def from_domain(cls, value: BrokerOperationsView) -> "OperationsResponseSchema":
        return cls(
            items=[
                OperationSchema(
                    broker_id=item.broker_id,
                    broker_name=item.broker_name,
                    operation_id=item.operation.operation_id,
                    account_id=item.operation.account_id,
                    operation_type=item.operation.operation_type,
                    state=item.operation.state,
                    occurred_at=item.operation.occurred_at,
                    payment=MoneySchema.from_domain(item.operation.payment),
                    price=MoneySchema.from_domain(item.operation.price),
                    quantity=item.operation.quantity,
                    commission=MoneySchema.from_domain(item.operation.commission),
                    instrument_id=item.operation.instrument_id,
                    ticker=item.operation.ticker,
                )
                for item in value.items
            ],
            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
        )
