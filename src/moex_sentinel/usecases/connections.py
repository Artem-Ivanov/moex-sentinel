"""User intent for checking a broker connection."""

from typing import Protocol

from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
from moex_sentinel.domain.connections import BrokerConnectionStatus
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.broker_errors import BrokerOperationError


class ConnectionServicePort(Protocol):
    async def check(self, broker_id: str) -> BrokerConnectionStatus: ...


class CheckBrokerConnectionUsecase:
    """Check a broker connection, distinguishing environment and configuration errors."""

    def __init__(self, service: ConnectionServicePort) -> None:
        self._service = service

    async def execute(self, broker_id: str) -> BrokerConnectionStatus:
        """Return connection status; translate known broker, environment and configuration failures."""
        try:
            return await self._service.check(broker_id)
        except BrokerOperationError as error:
            raise UseCaseError(error.code, str(error)) from error
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except EnvironmentMismatchError as error:
            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
        except ValueError as error:
            raise UseCaseError("BROKER_CONFIGURATION", str(error)) from error
