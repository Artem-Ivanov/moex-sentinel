"""Persistent market-only SDK session and history conversion."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from moex_sentinel.adapters.tinvest.market_stream_source import TInvestMarketStreamSource
from moex_sentinel.services.broker_factory import TINVEST_SANDBOX_TARGET

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


class Manager:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class Client:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.manager = Manager()
        self.market_data = self
        self.requests = []

    async def __aenter__(self):
        self.opens += 1
        return self

    async def __aexit__(self, *args):
        self.closes += 1

    def create_market_data_stream(self):
        return self.manager

    async def get_candles(self, **kwargs):
        self.requests.append(kwargs)
        price = SimpleNamespace(units=10, nano=0)
        return SimpleNamespace(
            candles=[
                SimpleNamespace(open=price, high=price, low=price, close=price, volume=1, time=NOW, is_complete=True)
            ]
        )


def test_market_source_reuses_session_and_releases_stream_and_client():
    async def run():
        client = Client()
        source = TInvestMarketStreamSource(
            "synthetic", TINVEST_SANDBOX_TARGET, client_factory=lambda *args, **kwargs: client
        )
        await source.start()
        await source.start()
        first = await source.get_candles("AAA", NOW, NOW)
        await source.get_candles("BBB", NOW, NOW)
        assert client.opens == 1
        assert [request["instrument_id"] for request in client.requests] == ["AAA", "BBB"]
        assert first[0].instrument_id == "AAA"
        assert first[0].started_at == NOW
        await source.close()
        await source.close()
        assert client.manager.stopped
        assert client.closes == 1

    asyncio.run(run())
