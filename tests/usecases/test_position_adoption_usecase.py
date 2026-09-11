"""Position adoption use-case boundary."""

import asyncio
from types import SimpleNamespace

from moex_sentinel.domain.position_adoption import PositionAdoptionResult
from moex_sentinel.usecases.position_adoption import AdoptBrokerPositionsUsecase


class Service:
    async def adopt(self, user_broker_id: str, account_id: str, broker: object) -> PositionAdoptionResult:
        assert user_broker_id == "broker-1"
        assert account_id == "account-1"
        assert broker == "adapter"
        return PositionAdoptionResult()


def test_usecase_resolves_active_broker_and_delegates() -> None:
    broker = SimpleNamespace(id="broker-1", external_account_id="account-1", state="ACTIVE")
    usecase = AdoptBrokerPositionsUsecase(Service(), lambda broker_id: broker, lambda value: "adapter")

    result = asyncio.run(usecase.execute("broker-1"))

    assert result == PositionAdoptionResult()
