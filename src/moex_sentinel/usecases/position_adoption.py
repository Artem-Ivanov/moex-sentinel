"""Use-case boundary for adopting current positions from one configured broker."""

from collections.abc import Callable
from typing import Protocol

from moex_sentinel.domain.position_adoption import PositionAdoptionResult
from moex_sentinel.services.portfolio_ports import PositionAdoptionBrokerPort


class PositionAdoptionServicePort(Protocol):
    async def adopt(
        self,
        user_broker_id: str,
        account_id: str,
        broker: PositionAdoptionBrokerPort,
    ) -> PositionAdoptionResult: ...


class AdoptBrokerPositionsUsecase:
    def __init__(
        self,
        service: PositionAdoptionServicePort,
        broker_loader: Callable[[str], object],
        adapter_factory: Callable[[object], PositionAdoptionBrokerPort],
    ) -> None:
        self._service = service
        self._broker_loader = broker_loader
        self._adapter_factory = adapter_factory

    async def execute(self, user_broker_id: str) -> PositionAdoptionResult:
        broker = self._broker_loader(user_broker_id)
        account_id = getattr(broker, "external_account_id", None)
        state = getattr(broker, "state", None)
        state_value = getattr(state, "value", state)
        if state_value != "ACTIVE" or not account_id:
            raise ValueError("Position adoption requires an active broker account.")
        return await self._service.adopt(
            user_broker_id,
            str(account_id),
            self._adapter_factory(broker),
        )
