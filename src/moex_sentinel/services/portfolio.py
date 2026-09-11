"""Aggregation of read-only portfolio data across enabled brokers."""

import asyncio
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any, Protocol

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker
from moex_sentinel.domain.instrument_catalog import UserBrokerCatalogInstrument
from moex_sentinel.domain.portfolio import (
    BrokerAccountSnapshot,
    BrokerAccountsView,
    BrokerOperation,
    BrokerOperationsView,
    BrokerPosition,
    BrokerPositionsView,
    BrokerReadError,
    ExternalOperation,
    ExternalPosition,
    Money,
)
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.portfolio_ports import PortfolioPort
from moex_sentinel.services.ports import BrokerRepositoryPort

AdapterFactory = Callable[[Broker], PortfolioPort]


class InstrumentCatalogLookupPort(Protocol):
    def find_by_external_instrument_id(
        self,
        user_broker_id: str,
        external_instrument_id: str,
    ) -> UserBrokerCatalogInstrument | None: ...


NON_TRADING_OPERATION_TYPES = frozenset(
    {
        "OPERATION_TYPE_INPUT",
        "OPERATION_TYPE_OUTPUT",
        "OPERATION_TYPE_INPUT_SECURITIES",
        "OPERATION_TYPE_OUTPUT_SECURITIES",
        "OPERATION_TYPE_INPUT_SWIFT",
        "OPERATION_TYPE_OUTPUT_SWIFT",
        "OPERATION_TYPE_INPUT_ACQUIRING",
        "OPERATION_TYPE_OUTPUT_ACQUIRING",
        "OPERATION_TYPE_INP_MULTI",
        "OPERATION_TYPE_OUT_MULTI",
    }
)
POSITION_TRADE_TYPES = frozenset({"BUY", "SELL", "OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL"})
EXECUTED_OPERATION_STATES = frozenset({"EXECUTED", "OPERATION_STATE_EXECUTED"})


class PortfolioAggregationService:
    def __init__(
        self,
        brokers: BrokerRepositoryPort,
        adapter_factory: AdapterFactory,
        environment: EnvironmentStatePort | None = None,
        instruments: InstrumentCatalogLookupPort | None = None,
    ) -> None:
        self._brokers = brokers
        self._adapter_factory = adapter_factory
        self._environment = environment
        self._instruments = instruments

    async def view_all_accounts(self) -> BrokerAccountsView:
        return self._accounts_view(await self._for_enabled_brokers(self._read_accounts))

    async def view_broker_accounts(self, broker_id: str) -> BrokerAccountsView:
        broker = self._brokers.get(broker_id)
        if broker.is_test is not self._active_test():
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        return self._accounts_view((await self._read_accounts(broker),))

    def _accounts_view(
        self,
        results: tuple[tuple[tuple[BrokerAccountSnapshot, ...], tuple[BrokerReadError, ...]], ...],
    ) -> BrokerAccountsView:
        accounts = tuple(item for batch, _ in results for item in batch)
        errors = tuple(error for _, batch_errors in results for error in batch_errors)
        return BrokerAccountsView(
            accounts,
            self._sum_money(item.portfolio.total_amount for item in accounts),
            self._sum_money(item.portfolio.free_cash for item in accounts),
            errors,
        )

    async def view_positions(self) -> BrokerPositionsView:
        results = await self._for_enabled_brokers(self._read_positions)
        return BrokerPositionsView(
            tuple(item for batch, _ in results for item in batch),
            tuple(error for _, errors in results for error in errors),
        )

    async def view_operations(self, limit: int) -> BrokerOperationsView:
        async def read(
            broker: Broker,
        ) -> tuple[tuple[BrokerOperation, ...], tuple[BrokerReadError, ...]]:
            return await self._read_operations(broker, limit)

        results = await self._for_enabled_brokers(read)
        items = tuple(item for batch, _ in results for item in batch)
        return BrokerOperationsView(
            tuple(sorted(items, key=lambda item: item.operation.occurred_at, reverse=True)[:limit]),
            tuple(error for _, errors in results for error in errors),
        )

    async def view_position_operations(
        self,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        limit: int,
    ) -> BrokerOperationsView:
        broker = self._brokers.get(broker_id)
        if not broker.enabled:
            raise ValueError("Подключение брокера отключено.")
        if broker.is_test is not self._active_test():
            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        try:
            page = await self._adapter_factory(broker).get_operations(account_id, None, limit, instrument_id)
        except (TInvestAdapterError, ValueError) as error:
            return BrokerOperationsView((), (self._error(broker, account_id, error),))
        items = tuple(
            BrokerOperation(broker.id, broker.display_name, item)
            for item in page.items
            if item.operation_type in POSITION_TRADE_TYPES and item.state in EXECUTED_OPERATION_STATES
        )
        return BrokerOperationsView(
            tuple(sorted(items, key=lambda item: item.operation.occurred_at, reverse=True)),
            (),
        )

    async def _for_enabled_brokers(
        self,
        reader: Callable[[Broker], Coroutine[Any, Any, tuple[tuple[Any, ...], tuple[BrokerReadError, ...]]]],
    ) -> tuple[tuple[tuple[Any, ...], tuple[BrokerReadError, ...]], ...]:
        active_test = self._active_test()
        brokers = tuple(broker for broker in self._brokers.list() if broker.enabled and broker.is_test is active_test)
        return tuple(await asyncio.gather(*(reader(broker) for broker in brokers)))

    def _active_test(self) -> bool:
        return self._environment is None or self._environment.view().active_environment == "TEST"

    async def _read_accounts(
        self, broker: Broker
    ) -> tuple[tuple[BrokerAccountSnapshot, ...], tuple[BrokerReadError, ...]]:
        try:
            adapter = self._adapter_factory(broker)
            accounts = await adapter.list_accounts()
        except (TInvestAdapterError, ValueError) as error:
            return (), (self._error(broker, None, error),)

        snapshots: list[BrokerAccountSnapshot] = []
        errors: list[BrokerReadError] = []
        for account in accounts:
            try:
                portfolio = await adapter.get_portfolio(account.account_id)
                snapshots.append(BrokerAccountSnapshot(broker.id, broker.display_name, account, portfolio))
            except TInvestAdapterError as error:
                errors.append(self._error(broker, account.account_id, error))
        return tuple(snapshots), tuple(errors)

    async def _read_positions(self, broker: Broker) -> tuple[tuple[BrokerPosition, ...], tuple[BrokerReadError, ...]]:
        try:
            adapter = self._adapter_factory(broker)
            accounts = await adapter.list_accounts()
        except (TInvestAdapterError, ValueError) as error:
            return (), (self._error(broker, None, error),)

        items: list[BrokerPosition] = []
        errors: list[BrokerReadError] = []
        for account in accounts:
            try:
                positions = await adapter.get_positions(account.account_id)
                items.extend(
                    BrokerPosition(broker.id, broker.display_name, item)
                    for item in positions
                    if self._is_open_instrument_position(item)
                )
            except TInvestAdapterError as error:
                errors.append(self._error(broker, account.account_id, error))
        return tuple(items), tuple(errors)

    async def _read_operations(
        self, broker: Broker, limit: int
    ) -> tuple[tuple[BrokerOperation, ...], tuple[BrokerReadError, ...]]:
        try:
            adapter = self._adapter_factory(broker)
            accounts = await adapter.list_accounts()
        except (TInvestAdapterError, ValueError) as error:
            return (), (self._error(broker, None, error),)

        items: list[BrokerOperation] = []
        errors: list[BrokerReadError] = []
        for account in accounts:
            try:
                page = await adapter.get_operations(account.account_id, None, limit)
                items.extend(
                    BrokerOperation(
                        broker.id,
                        broker.display_name,
                        self._with_ticker(broker.id, item),
                    )
                    for item in page.items
                    if item.operation_type not in NON_TRADING_OPERATION_TYPES
                )
            except TInvestAdapterError as error:
                errors.append(self._error(broker, account.account_id, error))
        return tuple(items), tuple(errors)

    def _with_ticker(self, broker_id: str, operation: ExternalOperation) -> ExternalOperation:
        if self._instruments is None or not operation.instrument_id:
            return operation
        instrument = self._instruments.find_by_external_instrument_id(broker_id, operation.instrument_id)
        if instrument is None:
            return operation
        return operation.model_copy(update={"ticker": instrument.ticker})

    @staticmethod
    def _sum_money(values: Any) -> tuple[Money, ...]:
        totals: dict[str, Decimal] = {}
        for value in values:
            if value is not None:
                totals[value.currency] = totals.get(value.currency, Decimal()) + value.amount
        return tuple(Money(amount, currency) for currency, amount in sorted(totals.items()))

    @staticmethod
    def _is_open_instrument_position(position: ExternalPosition) -> bool:
        return bool(position.instrument_id.strip() and position.ticker.strip() and position.quantity_lots != 0)

    @staticmethod
    def _error(
        broker: Broker,
        account_id: str | None,
        error: TInvestAdapterError | ValueError,
    ) -> BrokerReadError:
        if isinstance(error, TInvestAdapterError):
            return BrokerReadError(broker.id, broker.display_name, account_id, error.code, str(error))
        return BrokerReadError(broker.id, broker.display_name, account_id, "BROKER_CONFIGURATION", str(error))
