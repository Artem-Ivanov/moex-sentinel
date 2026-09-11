"""Worker intent for resolving one short-lived broker connection."""

from typing import Protocol

from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.broker_execution import BrokerConnection


class AutomatonBrokerServicePort(Protocol):
    def connection(self, broker_id: str) -> BrokerConnection: ...


class ViewAutomatonBrokerConnectionUsecase:
    def __init__(self, service: AutomatonBrokerServicePort) -> None:
        self._service = service

    def execute(self, broker_id: str) -> BrokerConnection:
        try:
            return self._service.connection(broker_id)
        except BrokerRecordNotFoundError as error:
            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
        except ValueError as error:
            raise UseCaseError(
                "BROKER_EXECUTION_UNAVAILABLE",
                "Подключение недоступно для тестовой торговли.",
            ) from error
