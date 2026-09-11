"""In-process broker portfolio state maintained outside the hot decision path."""

import asyncio
from collections.abc import Sequence

from sentinel_contracts.broker_execution import BrokerPosition


class PortfolioStateCacheService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._positions: dict[tuple[str, str], BrokerPosition] = {}

    async def replace_snapshot(self, account_id: str, positions: Sequence[BrokerPosition]) -> None:
        async with self._lock:
            retained = {key: value for key, value in self._positions.items() if key[0] != account_id}
            retained.update({(account_id, item.instrument_id): item for item in positions})
            self._positions = retained

    async def apply_position_event(self, account_id: str, position: BrokerPosition) -> None:
        async with self._lock:
            self._positions[(account_id, position.instrument_id)] = position

    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None:
        async with self._lock:
            return self._positions.get((account_id, instrument_id))
