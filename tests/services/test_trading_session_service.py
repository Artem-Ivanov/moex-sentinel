import asyncio
from types import SimpleNamespace

from moex_sentinel.services.trading_sessions import TradingSessionService
from sentinel_contracts.broker_execution import BrokerConnection, BrokerTradingStatus


class Automations:
    def list_active(self):
        return [
            SimpleNamespace(broker_id="b1", instrument_id="i1"),
            SimpleNamespace(broker_id="b1", instrument_id="i2"),
        ]


class Connections:
    def connection(self, broker_id: str):
        return BrokerConnection(broker_id, "TINVEST_SANDBOX", "sandbox", "test", True)


class Adapter:
    async def get_trading_status(self, instrument_id: str):
        opened = instrument_id == "i1"
        return BrokerTradingStatus("NORMAL" if opened else "CLOSED", opened, opened, True)


def test_aggregates_live_status_for_active_instruments() -> None:
    service = TradingSessionService(Automations(), Connections(), lambda _connection: Adapter())

    result = asyncio.run(service.status())

    assert result.status == "OPEN"
    assert (result.total, result.open, result.closed, result.unavailable) == (2, 1, 1, 0)
