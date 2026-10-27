"""Public schemas for SDK-neutral portfolio data."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from moex_sentinel.domain.portfolio import (
    BrokerAccountsView,
    BrokerOperation,
    BrokerOperationsPageView,
    BrokerOperationsView,
    BrokerPositionsView,
    Money,
)
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.time import is_historical, utc_now_ms


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
    historical: bool

    @classmethod
    def from_domain(cls, item: BrokerOperation, captured_at: datetime) -> "OperationSchema":
        operation = item.operation
        return cls(
            broker_id=item.broker_id,
            broker_name=item.broker_name,
            operation_id=operation.operation_id,
            account_id=operation.account_id,
            operation_type=operation.operation_type,
            state=operation.state,
            occurred_at=operation.occurred_at,
            payment=MoneySchema.from_domain(operation.payment),
            price=MoneySchema.from_domain(operation.price),
            quantity=operation.quantity,
            commission=MoneySchema.from_domain(operation.commission),
            instrument_id=operation.instrument_id,
            ticker=operation.ticker,
            historical=is_historical(operation.occurred_at, captured_at),
        )


class OperationsResponseSchema(StrictSchema):
    captured_at: datetime
    items: list[OperationSchema]
    errors: list[ReadErrorSchema]

    @classmethod
    def from_domain(cls, value: BrokerOperationsView) -> "OperationsResponseSchema":
        captured_at = utc_now_ms()
        return cls(
            captured_at=captured_at,
            items=[OperationSchema.from_domain(item, captured_at) for item in value.items],
            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
        )


class OperationsPageResponseSchema(StrictSchema):
    captured_at: datetime
    items: list[OperationSchema]
    errors: list[ReadErrorSchema]
    input_cursor: str | None
    next_cursor: str | None
    source_item_count: int
    selected_item_count: int
    source_exhausted: bool | None
    coverage_complete: bool

    @classmethod
    def from_domain(cls, value: BrokerOperationsPageView) -> "OperationsPageResponseSchema":
        captured_at = utc_now_ms()
        return cls(
            captured_at=captured_at,
            items=[OperationSchema.from_domain(item, captured_at) for item in value.items],
            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
            input_cursor=value.input_cursor,
            next_cursor=value.next_cursor,
            source_item_count=value.source_item_count,
            selected_item_count=value.selected_item_count,
            source_exhausted=value.source_exhausted,
            coverage_complete=value.coverage_complete,
        )
