"""SDK-neutral read-only portfolio records."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class Money(PositionalModel):
    model_config = ConfigDict(frozen=True)
    amount: Decimal
    currency: str


class BrokerAccount(PositionalModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    name: str
    status: str
    account_type: str


class AccountPortfolio(PositionalModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    total_amount: Money | None
    free_cash: Money | None
    realized_pnl: Money | None
    unrealized_pnl: Money | None


class ExternalPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    instrument_id: str
    ticker: str
    quantity_lots: Decimal
    average_price: Money | None
    current_price: Money | None
    expected_yield: Money | None


class ActiveBrokerOrder(PositionalModel):
    """Non-terminal or unknown broker order that blocks position adoption."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    instrument_id: str
    broker_order_id: str
    status: str


class ExternalOperation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    operation_id: str
    account_id: str
    operation_type: str
    state: str
    occurred_at: datetime
    payment: Money | None
    price: Money | None
    quantity: Decimal
    commission: Money | None
    instrument_id: str | None = None
    ticker: str | None = None


class OperationsPage(PositionalModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[ExternalOperation, ...]
    next_cursor: str | None


class BrokerReadError(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    broker_name: str
    account_id: str | None
    code: str
    message: str


class BrokerAccountSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    broker_name: str
    account: BrokerAccount
    portfolio: AccountPortfolio


class BrokerAccountsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    accounts: tuple[BrokerAccountSnapshot, ...]
    total_amounts: tuple[Money, ...]
    total_free_cash: tuple[Money, ...]
    errors: tuple[BrokerReadError, ...]


class BrokerPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    broker_name: str
    position: ExternalPosition


class BrokerPositionsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[BrokerPosition, ...]
    errors: tuple[BrokerReadError, ...]


class BrokerOperation(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    broker_name: str
    operation: ExternalOperation

    @property
    def account_id(self) -> str:
        return self.operation.account_id


class BrokerOperationsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[BrokerOperation, ...]
    errors: tuple[BrokerReadError, ...]
