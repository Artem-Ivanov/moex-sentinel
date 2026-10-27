"""Standalone application boundary for adopting configured broker positions."""

from moex_sentinel.domain.position_adoption import PositionAdoptionResult
from moex_sentinel.services.position_adoption import ConfiguredPositionAdoptionPort


class AdoptBrokerPositionsUsecase:
    """Expose configured position adoption without duplicating broker validation."""

    def __init__(self, service: ConfiguredPositionAdoptionPort) -> None:
        self._service = service

    async def execute(self, user_broker_id: str) -> PositionAdoptionResult:
        """Return adoption diagnostics or propagate the configured service failure."""
        return await self._service.adopt(user_broker_id)
