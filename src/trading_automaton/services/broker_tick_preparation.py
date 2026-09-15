"""Prepare broker and durable caches before the SLA market snapshot."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.streaming_market import MarketBatchSnapshot
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import CommissionQuote, CommissionRefreshRequest
from trading_automaton.services.account_commission_profile import (
    AccountCommissionProfileService,
)
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.storage.repository import AccountCommissionProfileKey

LOGGER = logging.getLogger(__name__)


class PreparationBrokerPort(Protocol):
    async def get_positions(self, account_id: str) -> Sequence[BrokerPosition]: ...

    async def get_free_cash(self, account_id: str, currency: str) -> Decimal: ...

    async def quote(self, request: CommissionRefreshRequest, side: str) -> CommissionQuote: ...


class PreparationPortfolioPort(Protocol):
    async def replace_snapshot(self, account_id: str, positions: Sequence[BrokerPosition]) -> None: ...

    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...

    async def apply_position_event(self, account_id: str, position: BrokerPosition) -> None: ...


class PreparationBootstrapPort(Protocol):
    async def bootstrap(self, instrument_id: str) -> None: ...


class PreparationHydrationPort(Protocol):
    async def hydrate(self, commands: tuple[AutomationCommand, ...]) -> None: ...


class PreparationCashPort(Protocol):
    async def replace_snapshot(self, account_id: str, currency: str, free_cash: Decimal) -> None: ...


class PreparationReconciliationPort(Protocol):
    async def reconcile(self, commands: tuple[AutomationCommand, ...]) -> object: ...


class BrokerTickPreparationService:
    def __init__(
        self,
        broker: PreparationBrokerPort,
        portfolio: PreparationPortfolioPort,
        bootstrap: PreparationBootstrapPort | None,
        commissions: AccountCommissionProfileService,
        hydration: PreparationHydrationPort,
        *,
        reconciliation: PreparationReconciliationPort | None = None,
        cash: PreparationCashPort | None = None,
        position_bootstrap: PositionBootstrapService | None = None,
        strategy_settings: StrategySettings | None = None,
        now: Callable[[], datetime],
    ) -> None:
        self._broker = broker
        self._portfolio = portfolio
        self._bootstrap = bootstrap
        self._commissions = commissions
        self._hydration = hydration
        self._reconciliation = reconciliation
        self._cash = cash
        self._now = now
        self._position_bootstrap = position_bootstrap
        self._strategy_settings = strategy_settings or StrategySettings()

    async def prepare(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> None:
        pending = tuple(
            item
            for item in commands
            if item.state in {AutomationState.HOLD, AutomationState.IN_QUEUE} and item.bootstrap is not None
        )
        regular = tuple(item for item in commands if item not in pending)
        if regular:
            await self._prepare_regular(regular, snapshot)
        # HOLD still supervises orders that may finish after the polling window.
        if pending and self._reconciliation is not None:
            await self._reconciliation.reconcile(pending)
        for command in pending:
            try:
                await self._prepare_adopted(command)
            except ValueError:
                LOGGER.warning("Position bootstrap remains pending", extra={"reason_code": "BOOTSTRAPPING"})

    async def _prepare_regular(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> None:
        account_ids = tuple(dict.fromkeys(item.account_id for item in commands))
        await asyncio.gather(*(self._load_account(account_id) for account_id in account_ids))
        if self._reconciliation is not None:
            await self._reconciliation.reconcile(commands)
        await self._load_cash(commands)
        await asyncio.gather(*(self._prepare_command(item, snapshot) for item in commands))
        await self._hydration.hydrate(commands)

    async def _prepare_adopted(self, command: AutomationCommand) -> None:
        bootstrap = self._position_bootstrap
        expected = command.bootstrap
        if bootstrap is None or expected is None:
            return
        # Adoption records an existing broker position. Quote freshness and order
        # commissions gate subsequent trading, not this accounting transaction.
        await self._load_account(command.account_id)
        position = await self._portfolio.position(command.account_id, command.external_instrument_id)
        if (
            position is None
            or position.quantity_lots != expected.quantity_lots
            or position.average_price != expected.average_price
            or position.currency.upper() != expected.currency.upper()
        ):
            return
        await asyncio.to_thread(bootstrap.ensure, command, self._strategy_settings)

    async def _load_account(self, account_id: str) -> None:
        positions = await self._broker.get_positions(account_id)
        await self._portfolio.replace_snapshot(account_id, positions)

    async def _load_cash(self, commands: tuple[AutomationCommand, ...]) -> None:
        cash = self._cash
        if cash is None:
            return
        accounts = {(item.account_id, item.currency) for item in commands}

        async def load(account_id: str, currency: str) -> None:
            free_cash = await self._broker.get_free_cash(account_id, currency)
            await cash.replace_snapshot(account_id, currency, free_cash)

        await asyncio.gather(*(load(account_id, currency) for account_id, currency in accounts))

    async def _prepare_command(
        self,
        command: AutomationCommand,
        snapshot: MarketBatchSnapshot,
    ) -> None:
        market = snapshot.instruments.get(command.external_instrument_id)
        if market is None or market.order_book is None or not market.order_book.asks:
            return
        position = await self._portfolio.position(command.account_id, command.external_instrument_id)
        if position is None:
            await self._portfolio.apply_position_event(
                command.account_id,
                BrokerPosition(
                    command.external_instrument_id,
                    Decimal(),
                    Decimal(),
                    market.order_book.best_ask.price,
                    command.currency,
                ),
            )
        if self._bootstrap is not None:
            await self._bootstrap.bootstrap(command.external_instrument_id)
        await self._commissions.refresh_if_due(
            CommissionRefreshRequest(
                AccountCommissionProfileKey(
                    str(command.broker_id),
                    command.account_id,
                    "SHARE",
                    command.currency,
                ),
                command.external_instrument_id,
                1,
                market.order_book.best_ask.price,
            ),
            self._broker,
            now=self._now(),
        )
