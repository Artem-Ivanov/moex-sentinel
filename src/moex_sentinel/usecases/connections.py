"""User intent for checking a broker connection."""

from typing import Protocol

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
from moex_sentinel.domain.connections import BrokerConnectionStatus
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.usecases.errors import UseCaseError


class ConnectionServicePort(Protocol):
    async def check(self, broker_id: str) -> BrokerConnectionStatus: ...


class CheckBrokerConnectionUsecase:
    def __init__(self, service: ConnectionServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str) -> BrokerConnectionStatus:
        try:
            return await self._service.check(broker_id)
        except TInvestAdapterError as error:
            raise UseCaseError(error.code, str(error)) from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except ValueError as error:
            if isinstance(error, EnvironmentMismatchError):
                raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error
