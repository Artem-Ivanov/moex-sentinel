import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import (
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)
from trading_automaton.services.hot_market_data import HotMarketDataCacheService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def order_book(
    instrument_id: str,
    *,
    captured_at: datetime = NOW,
    is_consistent: bool = True,
) -> StreamOrderBook:
    return StreamOrderBook(
        instrument_id=instrument_id,
        bids=(OrderBookLevel(Decimal("100"), 10),),
        asks=(OrderBookLevel(Decimal("100.1"), 10),),
        captured_at=captured_at,
        is_consistent=is_consistent,
    )


def trading_status(instrument_id: str, *, captured_at: datetime = NOW) -> StreamTradingStatus:
    return StreamTradingStatus(
        instrument_id=instrument_id,
        status="SECURITY_TRADING_STATUS_NORMAL_TRADING",
        limit_order_available=True,
        api_trade_available=True,
        captured_at=captured_at,
    )


def test_snapshot_is_atomic_and_uses_latest_events() -> None:
    async def scenario():
        cache = HotMarketDataCacheService()
        await cache.update_order_book(order_book("i1"))
        await cache.update_last_price(StreamLastPrice("i1", Decimal("99"), NOW))
        await cache.update_last_price(StreamLastPrice("i1", Decimal("100.05"), NOW))
        await cache.update_trading_status(trading_status("i1"))

        return await cache.snapshot(("i1", "i2"), created_at=NOW)

    snapshot = asyncio.run(scenario())

    assert snapshot.snapshot_id
    assert snapshot.created_at == NOW
    assert snapshot.instruments["i1"].order_book.best_ask.price == Decimal("100.1")
    assert snapshot.instruments["i1"].last_price.price == Decimal("100.05")
    assert snapshot.instruments["i1"].is_tradeable(NOW, timedelta(seconds=1))
    assert not snapshot.instruments["i2"].is_tradeable(NOW, timedelta(seconds=1))


def test_stale_or_inconsistent_order_book_is_not_tradeable() -> None:
    async def scenario(book: StreamOrderBook):
        cache = HotMarketDataCacheService()
        await cache.update_order_book(book)
        await cache.update_trading_status(trading_status("i1"))
        return await cache.snapshot(("i1",), created_at=NOW)

    stale = asyncio.run(scenario(order_book("i1", captured_at=NOW - timedelta(seconds=2))))
    inconsistent = asyncio.run(scenario(order_book("i1", is_consistent=False)))

    assert not stale.instruments["i1"].is_tradeable(NOW, timedelta(seconds=1))
    assert not inconsistent.instruments["i1"].is_tradeable(NOW, timedelta(seconds=1))


def test_status_remains_tradeable_for_the_stream_session() -> None:
    async def scenario():
        cache = HotMarketDataCacheService()
        await cache.update_order_book(order_book("i1"))
        await cache.update_trading_status(trading_status("i1", captured_at=NOW - timedelta(hours=1)))
        return await cache.snapshot(("i1",), created_at=NOW)

    snapshot = asyncio.run(scenario())

    assert snapshot.instruments["i1"].is_tradeable(NOW, timedelta(seconds=1))


def test_snapshot_mapping_is_immutable() -> None:
    async def scenario():
        cache = HotMarketDataCacheService()
        return await cache.snapshot(("i1",), created_at=NOW)

    snapshot = asyncio.run(scenario())

    try:
        snapshot.instruments["i2"] = snapshot.instruments["i1"]  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("Snapshot mapping must be immutable.")
