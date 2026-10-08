"""Persistent market-only SDK session and history conversion."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from grpc import StatusCode
from t_tech.invest.exceptions import AioRequestError

from moex_sentinel.adapters.tinvest.market_stream_source import TInvestMarketStreamSource
from moex_sentinel.services.broker_factory import TINVEST_SANDBOX_TARGET
from sentinel_contracts.broker_errors import BrokerOperationError

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


def failing_source(operation, error):
    class FailingManager(Manager):
        def __init__(self):
            super().__init__()
            self.order_book = self.last_price = self.candles = self.info = self

        def subscribe(self, _instruments):
            if operation == "subscription":
                raise error

        def waiting_close(self, _enabled):
            return self

        async def __aiter__(self):
            if operation == "events":
                raise error
            return
            yield  # pragma: no cover - make the SDK iterator an async generator

    class FailingClient(Client):
        def __init__(self):
            super().__init__()
            self.manager = FailingManager()

        async def __aenter__(self):
            if operation == "start":
                raise error
            return await super().__aenter__()

        async def __aexit__(self, *args):
            if operation == "close":
                raise error
            return await super().__aexit__(*args)

        async def get_candles(self, **kwargs):
            if operation == "history":
                raise error
            return await super().get_candles(**kwargs)

    return TInvestMarketStreamSource(
        "synthetic", TINVEST_SANDBOX_TARGET, client_factory=lambda *_args, **_kwargs: FailingClient()
    )


async def run_source_operation(source, operation):
    try:
        await source.start()
        if operation == "subscription":
            await source.replace_subscriptions({"AAA"})
        elif operation == "events":
            async for _event in source.events():
                pass
        elif operation == "history":
            await source.get_candles("AAA", NOW, NOW)
        elif operation == "close":
            await source.close()
    finally:
        await source.close()


@pytest.mark.parametrize("operation", ["start", "subscription", "events", "history", "close"])
@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (StatusCode.UNAUTHENTICATED, False),
        (StatusCode.PERMISSION_DENIED, False),
        (StatusCode.INVALID_ARGUMENT, False),
        (StatusCode.FAILED_PRECONDITION, False),
        (StatusCode.UNIMPLEMENTED, False),
        (StatusCode.INTERNAL, True),
        (StatusCode.UNAVAILABLE, True),
    ],
)
def test_market_sdk_boundary_preserves_retry_policy_without_private_details(operation, status, retryable):
    error = AioRequestError(status, "private-token-detail", {"private": "private-token-metadata"})

    with pytest.raises(BrokerOperationError) as caught:
        asyncio.run(run_source_operation(failing_source(operation, error), operation))
    assert caught.value.retryable is retryable
    assert "private-token" not in str(caught.value)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("operation", ["start", "subscription", "events", "history", "close"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_market_source_does_not_translate_unexpected_errors_or_cancellation(operation, error_type):
    error = error_type("synthetic failure")
    with pytest.raises(error_type) as caught:
        asyncio.run(run_source_operation(failing_source(operation, error), operation))
    assert caught.value is error
