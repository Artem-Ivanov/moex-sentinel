"""Aggregate live trading-session availability for active automations."""

import asyncio
from collections.abc import Callable, Sequence
from time import monotonic
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
    """Cache the UI summary for 60 seconds while revalidating its broker scope."""

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
        self._lock = asyncio.Lock()
        self._cached_targets: tuple[tuple[BrokerConnection, str] | None, ...] = ()
        self._cached_status: TradingSessionsStatus | None = None
        self._expires_at = 0.0

    async def status(self) -> TradingSessionsStatus:
        """Coalesce readers; never reuse failed reads or a changed configuration."""
        async with self._lock:
            return await self._status()

    async def _status(self) -> TradingSessionsStatus:
        targets = sorted({(item.broker_id, item.instrument_id) for item in self._automations.list_active()})
        if not targets:
            self._cached_status = None
            self._cached_targets = ()
            return TradingSessionsStatus(status="NO_ACTIVE", total=0, open=0, closed=0, unavailable=0)
        resolved = tuple(self._resolve(broker_id, instrument_id) for broker_id, instrument_id in targets)
        if self._cached_status is not None and resolved == self._cached_targets and monotonic() < self._expires_at:
            return self._cached_status
        self._cached_status = None
        self._cached_targets = ()
        results = await asyncio.gather(*(self._read(target) for target in resolved))
        opened = results.count("OPEN")
        closed = results.count("CLOSED")
        unavailable = results.count("UNAVAILABLE")
        aggregate = "OPEN" if opened else ("UNAVAILABLE" if unavailable else "CLOSED")
        result = TradingSessionsStatus(
            status=aggregate,
            total=len(targets),
            open=opened,
            closed=closed,
            unavailable=unavailable,
        )
        if unavailable == 0:
            self._cached_targets = resolved
            self._cached_status = result
            self._expires_at = monotonic() + 60.0
        return result

    def _resolve(self, broker_id: str, instrument_id: str) -> tuple[BrokerConnection, str] | None:
        """Resolve live configuration before consulting the successful summary cache."""
        try:
            instrument = self._catalog.get(broker_id, instrument_id)
            connection = self._connections.connection(broker_id)
            return connection, instrument.external_instrument_id
        except Exception:
            return None

    async def _read(self, target: tuple[BrokerConnection, str] | None) -> str:
        """Read one broker status, keeping unavailable instruments in the aggregate."""
        if target is None:
            return "UNAVAILABLE"
        connection, instrument_id = target
        try:
            status = await self._adapter_builder(connection).get_trading_status(instrument_id)
        except Exception:
            return "UNAVAILABLE"
        return "OPEN" if status.api_trade_available and status.limit_order_available else "CLOSED"
