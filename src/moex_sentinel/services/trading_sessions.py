"""Aggregate live trading-session availability for active automations."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Protocol

from moex_sentinel.domain.instrument_catalog import UserBrokerCatalogInstrument
from moex_sentinel.domain.trading_sessions import TradingSessionsStatus
from sentinel_contracts.broker_execution import BrokerConnection, BrokerTradingStatus


class ActiveAutomation(Protocol):
    broker_id: str
    instrument_id: str


class AutomationPort(Protocol):
    def list_active(self) -> Sequence[ActiveAutomation]: ...


class ConnectionPort(Protocol):
    def connection(self, broker_id: str) -> BrokerConnection: ...


class TradingStatusPort(Protocol):
    async def get_trading_status(self, instrument_id: str) -> BrokerTradingStatus: ...


class InstrumentCatalogPort(Protocol):
    def get(self, user_broker_id: str, instrument_id: str) -> UserBrokerCatalogInstrument: ...


class TradingSessionService:
    def __init__(
        self,
        automations: AutomationPort,
        connections: ConnectionPort,
        adapter_builder: Callable[[BrokerConnection], TradingStatusPort],
        catalog: InstrumentCatalogPort,
    ) -> None:
        self._automations = automations
        self._connections = connections
        self._adapter_builder = adapter_builder
        self._catalog = catalog

    async def status(self) -> TradingSessionsStatus:
        targets = sorted({(item.broker_id, item.instrument_id) for item in self._automations.list_active()})
        if not targets:
            return TradingSessionsStatus(status="NO_ACTIVE", total=0, open=0, closed=0, unavailable=0)
        results = await asyncio.gather(*(self._read(broker_id, instrument_id) for broker_id, instrument_id in targets))
        opened = results.count("OPEN")
        closed = results.count("CLOSED")
        unavailable = results.count("UNAVAILABLE")
        aggregate = "OPEN" if opened else ("UNAVAILABLE" if unavailable else "CLOSED")
        return TradingSessionsStatus(
            status=aggregate,
            total=len(targets),
            open=opened,
            closed=closed,
            unavailable=unavailable,
        )

    async def _read(self, broker_id: str, instrument_id: str) -> str:
        try:
            instrument = self._catalog.get(broker_id, instrument_id)
            connection = self._connections.connection(broker_id)
            status = await self._adapter_builder(connection).get_trading_status(instrument.external_instrument_id)
        except Exception:
            return "UNAVAILABLE"
        return "OPEN" if status.api_trade_available and status.limit_order_available else "CLOSED"
