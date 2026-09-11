import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from t_tech.invest.grpc.schemas import Order, OrderBook, Quotation

from sentinel_contracts.streaming_market import StreamOrderBook
from trading_automaton.adapters.tinvest_streaming import TInvestStreamingAdapter

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class FakeSubscription:
    def __init__(self) -> None:
        self.subscribed = []
        self.unsubscribed = []

    def subscribe(self, instruments):
        self.subscribed.extend(instruments)

    def unsubscribe(self, instruments):
        self.unsubscribed.extend(instruments)

    def waiting_close(self, enabled=True):
        assert enabled
        return self


class FakeManager:
    def __init__(self, responses=()) -> None:
        self.order_book = FakeSubscription()
        self.last_price = FakeSubscription()
        self.candles = FakeSubscription()
        self.info = FakeSubscription()
        self.responses = tuple(responses)
        self.stopped = False

    def __aiter__(self):
        async def iterate():
            for item in self.responses:
                yield item

        return iterate()

    def stop(self):
        self.stopped = True


def test_replaces_subscriptions_by_diff_for_all_required_streams() -> None:
    async def scenario():
        manager = FakeManager()
        adapter = TInvestStreamingAdapter(manager)
        await adapter.replace_subscriptions({"i1", "i2"})
        await adapter.replace_subscriptions({"i2", "i3"})
        return manager

    manager = asyncio.run(scenario())

    assert {item.instrument_id for item in manager.order_book.subscribed} == {
        "i1",
        "i2",
        "i3",
    }
    assert all(item.depth == 20 for item in manager.order_book.subscribed)
    assert [item.instrument_id for item in manager.order_book.unsubscribed] == ["i1"]
    assert {item.instrument_id for item in manager.last_price.subscribed} == {
        "i1",
        "i2",
        "i3",
    }
    assert {item.instrument_id for item in manager.info.subscribed} == {
        "i1",
        "i2",
        "i3",
    }
    assert {item.instrument_id for item in manager.candles.subscribed} == {
        "i1",
        "i2",
        "i3",
    }


def test_converts_consistent_order_book_response() -> None:
    response = SimpleNamespace(
        orderbook=OrderBook(
            instrument_uid="i1",
            depth=20,
            is_consistent=True,
            bids=[Order(Quotation(units=100, nano=0), 10)],
            asks=[Order(Quotation(units=100, nano=100_000_000), 12)],
            time=NOW,
        ),
        last_price=None,
        trading_status=None,
        candle=None,
    )

    async def scenario():
        adapter = TInvestStreamingAdapter(FakeManager((response,)))
        return [item async for item in adapter.events()]

    events = asyncio.run(scenario())

    assert events == [
        StreamOrderBook(
            "i1",
            events[0].bids,
            events[0].asks,
            NOW,
            True,
        )
    ]
    assert events[0].best_ask.price == Decimal("100.1")


def test_close_stops_stream_manager() -> None:
    manager = FakeManager()
    adapter = TInvestStreamingAdapter(manager)

    asyncio.run(adapter.close())

    assert manager.stopped
