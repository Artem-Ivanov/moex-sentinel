import asyncio
from dataclasses import dataclass

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.domain.connections import BrokerConnectionStatus
from moex_sentinel.usecases.connections import CheckBrokerConnectionUsecase
from moex_sentinel.usecases.errors import UseCaseError


@dataclass
class ConnectionService:
    result: BrokerConnectionStatus | Exception

    async def check(self, broker_id: str) -> BrokerConnectionStatus:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_check_connection_returns_safe_status() -> None:
    expected = BrokerConnectionStatus("broker-1", True, 2)
    usecase = CheckBrokerConnectionUsecase(ConnectionService(expected))

    assert asyncio.run(usecase.execute("broker-1")) == expected


def test_check_connection_maps_adapter_error_to_usecase_error() -> None:
    error = TInvestAdapterError("BROKER_AUTH_FAILED", "Проверка не пройдена.", retryable=False)
    usecase = CheckBrokerConnectionUsecase(ConnectionService(error))

    with pytest.raises(UseCaseError) as caught:
        asyncio.run(usecase.execute("broker-1"))

    assert caught.value.code == "BROKER_AUTH_FAILED"
    assert "broker-1" not in caught.value.message
