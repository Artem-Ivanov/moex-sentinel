"""Business service for checking configured broker connectivity."""

from collections.abc import Callable

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import Broker
from moex_sentinel.domain.connections import BrokerConnectionStatus
from moex_sentinel.services.environment import EnvironmentMismatchError, EnvironmentStatePort
from moex_sentinel.services.portfolio_ports import PortfolioPort
from moex_sentinel.services.ports import BrokerRepositoryPort


class BrokerConnectionService:
    def __init__(
        self,
        brokers: BrokerRepositoryPort,
        adapter_factory: Callable[[Broker], PortfolioPort],
        environment: EnvironmentStatePort | None = None,
    ) -> None:
        self._brokers = brokers
        self._adapter_factory = adapter_factory
        self._environment = environment

    async def check(self, broker_id: str) -> BrokerConnectionStatus:
        broker = self._brokers.get(broker_id)
        if self._environment is not None:
            active_test = self._environment.view().active_environment == "TEST"
            if broker.is_test is not active_test:
                raise EnvironmentMismatchError("Broker belongs to inactive environment.")
        adapter = self._adapter_factory(broker)
        for attempt in range(2):
            try:
                accounts = await adapter.list_accounts()
                return BrokerConnectionStatus(broker.id, True, len(accounts))
            except TInvestAdapterError as error:
                if not error.retryable or attempt == 1:
                    raise
        raise RuntimeError("Unreachable connection check state.")
