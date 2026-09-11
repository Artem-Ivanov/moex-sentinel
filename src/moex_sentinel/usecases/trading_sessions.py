"""User intent for viewing current trading-session availability."""

from typing import Protocol

from moex_sentinel.domain.trading_sessions import TradingSessionsStatus


class TradingSessionServicePort(Protocol):
    async def status(self) -> TradingSessionsStatus: ...


class ViewTradingSessionsStatusUsecase:
    def __init__(self, service: TradingSessionServicePort) -> None:
        self._service = service

    async def execute(self) -> TradingSessionsStatus:
        return await self._service.status()
