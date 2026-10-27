"""Business service for checking configured broker connectivity."""

from collections.abc import Callable
from functools import partial

from moex_sentinel.domain.connections import BrokerConnectionStatus
from moex_sentinel.domain.user_brokers import UserBroker
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.portfolio_ports import PortfolioPort
from moex_sentinel.services.ports import UserBrokerLookupPort
from moex_sentinel.services.sync_execution import run_sync
from sentinel_contracts.broker_errors import BrokerOperationError


class BrokerConnectionService:
    def __init__(
        self,
        brokers: UserBrokerLookupPort,
        adapter_factory: Callable[[UserBroker], PortfolioPort],
        environment: EnvironmentStatePort | None = None,
    ) -> None:
        self._brokers = brokers
        self._adapter_factory = adapter_factory
        self._environment = environment

    async def check(self, broker_id: str) -> BrokerConnectionStatus:
        broker = await run_sync(partial(self._broker, broker_id))
        adapter = self._adapter_factory(broker)
        for attempt in range(2):
            try:
                accounts = await adapter.list_accounts()
                if broker.account_id and not any(account.account_id == broker.account_id for account in accounts):
                    raise BrokerOperationError(  # noqa: TRY301 - admission belongs before the successful result.
                        "BROKER_ACCOUNT_NOT_FOUND", "Выбранный счёт не найден на площадке.", retryable=False
                    )
                return BrokerConnectionStatus(broker.id, True, len(accounts))
            except BrokerOperationError as error:
                if not error.retryable or attempt == 1:
                    raise
        raise RuntimeError("Unreachable connection check state.")

    def _broker(self, broker_id: str) -> UserBroker:
        broker = self._brokers.get(broker_id)
        if self._environment is not None:
            active_test = self._environment.view().active_environment == "TEST"
            if broker.is_test is not active_test:
                raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        return broker
