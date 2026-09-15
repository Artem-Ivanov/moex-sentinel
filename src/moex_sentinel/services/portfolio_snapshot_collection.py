"""Periodic account-isolated portfolio snapshot collection."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, TypeVar
from uuid import uuid4

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker
from moex_sentinel.domain.portfolio import BrokerReadError, ExternalOperation
from moex_sentinel.domain.trading_summary import (
    PortfolioSnapshotValue,
    advance_cumulative_pnl,
    cash_flows,
)
from moex_sentinel.services.portfolio_ports import PortfolioPort
from moex_sentinel.services.ports import BrokerRepositoryPort
from sentinel_contracts.time import floor_utc_millisecond

AdapterFactory = Callable[[Broker], PortfolioPort]
Sleep = Callable[[float], Awaitable[None]]
ValueT = TypeVar("ValueT")
_STORAGE_QUANTUM = Decimal("0.000000001")
_STORAGE_ABS_LIMIT = Decimal("10000000000000000000")


def _is_storable_decimal(value: Decimal, *, non_negative: bool) -> bool:
    if not value.is_finite() or abs(value) >= _STORAGE_ABS_LIMIT:
        return False
    if non_negative and value < 0:
        return False
    try:
        return value.quantize(_STORAGE_QUANTUM) == value
    except InvalidOperation:
        return False


class LatestPortfolioSnapshotPort(Protocol):
    def latest(self, user_broker_id: str, account_id: str, currency: str) -> PortfolioSnapshotValue | None:
        """Read the previous persisted snapshot for one account and currency."""
        ...


class PortfolioSnapshotCollectionService:
    """Read and validate account snapshots without owning storage transactions."""

    def __init__(
        self,
        brokers: BrokerRepositoryPort,
        adapter_factory: AdapterFactory,
        history: LatestPortfolioSnapshotPort,
        *,
        clock: Callable[[], datetime],
        retry_limit: int = 2,
        retry_base_seconds: float = 1.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._brokers = brokers
        self._adapter_factory = adapter_factory
        self._history = history
        self._clock = clock
        self._retry_limit = retry_limit
        self._retry_base_seconds = retry_base_seconds
        self._sleep = sleep

    async def collect(
        self, run_id: str, captured_at: datetime, bucket_start: datetime
    ) -> tuple[tuple[PortfolioSnapshotValue, ...], tuple[BrokerReadError, ...]]:
        """Read configured accounts with isolated failures; return validated values and safe errors."""
        snapshots: list[PortfolioSnapshotValue] = []
        errors: list[BrokerReadError] = []
        for broker in self._brokers.list():
            if not broker.enabled:
                continue
            try:
                adapter = self._adapter_factory(broker)
                accounts = await self._retry(adapter.list_accounts)
            except TInvestAdapterError as error:
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=None,
                        code=error.code,
                        message=str(error),
                    )
                )
                continue
            except ValueError as error:
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=None,
                        code="BROKER_CONFIGURATION",
                        message=str(error),
                    )
                )
                continue
            account_id = broker.account_id
            if not account_id:
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=None,
                        code="BROKER_CONFIGURATION",
                        message="Для брокера не выбран счёт.",
                    )
                )
                continue
            if not any(account.account_id == account_id for account in accounts):
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=account_id,
                        code="BROKER_ACCOUNT_NOT_FOUND",
                        message="Выбранный счёт не найден на площадке.",
                    )
                )
                continue
            try:
                account_read_started_at = floor_utc_millisecond(self._clock())
                portfolio = await self._retry(
                    lambda adapter=adapter, account_id=account_id: adapter.get_portfolio(account_id)
                )
                account_read_completed_at = floor_utc_millisecond(self._clock())
            except TInvestAdapterError as error:
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=account_id,
                        code=error.code,
                        message=str(error),
                    )
                )
                continue
            total = portfolio.total_amount
            free_cash = portfolio.free_cash
            if (
                total is None
                or free_cash is None
                or total.currency.upper() != free_cash.currency.upper()
                or account_read_started_at < captured_at
                or account_read_completed_at < account_read_started_at
                or not _is_storable_decimal(total.amount, non_negative=True)
                or not _is_storable_decimal(free_cash.amount, non_negative=True)
            ):
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=account_id,
                        code="INVALID_PORTFOLIO_SNAPSHOT",
                        message="Площадка вернула несогласованные значения портфеля.",
                    )
                )
                continue
            previous = self._history.latest(broker.id, account_id, total.currency)
            cumulative_pnl = 0
            operations: tuple[ExternalOperation, ...] = ()
            operations_from = captured_at if previous is None else previous.captured_at
            if operations_from < account_read_completed_at:
                try:
                    operations = await self._operations(
                        adapter,
                        account_id,
                        operations_from,
                        account_read_completed_at,
                    )
                except TInvestAdapterError as error:
                    errors.append(
                        BrokerReadError(
                            broker_id=broker.id,
                            broker_name=broker.display_name,
                            account_id=account_id,
                            code=error.code,
                            message=str(error),
                        )
                    )
                    continue
            uncertain_operations = tuple(
                operation for operation in operations if floor_utc_millisecond(operation.occurred_at) > captured_at
            )
            uncertain_flows = cash_flows(uncertain_operations, currency=total.currency)
            if uncertain_flows.deposits or uncertain_flows.withdrawals:
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=account_id,
                        code="AMBIGUOUS_PORTFOLIO_SNAPSHOT",
                        message="Денежное движение пересекло границу снимка; счёт будет прочитан повторно.",
                    )
                )
                continue
            if previous is not None:
                stable_operations = tuple(
                    operation for operation in operations if floor_utc_millisecond(operation.occurred_at) <= captured_at
                )
                flows = cash_flows(stable_operations, currency=total.currency)
                cumulative_pnl = advance_cumulative_pnl(
                    previous_cumulative=previous.cumulative_pnl,
                    previous_value=previous.total_value,
                    current_value=total.amount,
                    deposits=flows.deposits,
                    withdrawals=flows.withdrawals,
                )
            if not _is_storable_decimal(Decimal(cumulative_pnl), non_negative=False):
                errors.append(
                    BrokerReadError(
                        broker_id=broker.id,
                        broker_name=broker.display_name,
                        account_id=account_id,
                        code="INVALID_PORTFOLIO_SNAPSHOT",
                        message="Площадка вернула значения портфеля вне поддерживаемого диапазона.",
                    )
                )
                continue
            snapshots.append(
                PortfolioSnapshotValue(
                    id=str(uuid4()),
                    run_id=run_id,
                    user_broker_id=broker.id,
                    account_id=account_id,
                    currency=total.currency.upper(),
                    total_value=total.amount,
                    free_cash=free_cash.amount,
                    cumulative_pnl=cumulative_pnl,
                    captured_at=captured_at,
                    bucket_start=bucket_start,
                    created_at=captured_at,
                )
            )
        return tuple(snapshots), tuple(errors)

    async def _retry(self, call: Callable[[], Awaitable[ValueT]]) -> ValueT:
        """Retry only declared retryable broker errors with the configured bounded backoff."""
        for attempt in range(self._retry_limit + 1):
            try:
                return await call()
            except TInvestAdapterError as error:
                if not error.retryable or attempt == self._retry_limit:
                    raise
                await self._sleep(self._retry_base_seconds * (2**attempt))
        raise AssertionError("unreachable")

    async def _operations(
        self,
        adapter: PortfolioPort,
        account_id: str,
        from_at: datetime,
        to_at: datetime,
    ) -> tuple[ExternalOperation, ...]:
        """Read every operations page within the original exclusive/inclusive time bounds."""
        items: list[ExternalOperation] = []
        cursor = None
        while True:
            page = await self._retry(
                lambda adapter=adapter, cursor=cursor: adapter.get_operations(
                    account_id,
                    cursor,
                    1000,
                    from_at=from_at,
                    to_at=to_at,
                )
            )
            items.extend(item for item in page.items if from_at < floor_utc_millisecond(item.occurred_at) <= to_at)
            if page.next_cursor is None:
                return tuple(items)
            cursor = page.next_cursor
