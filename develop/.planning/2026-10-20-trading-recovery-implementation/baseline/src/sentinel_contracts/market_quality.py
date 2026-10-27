"""Shared structural market invariants; freshness and trading policy remain with callers."""

from collections.abc import Iterable
from itertools import pairwise

from sentinel_contracts.streaming_market import StreamCandle, StreamOrderBook


def valid_market_structure(book: StreamOrderBook, candles: Iterable[StreamCandle]) -> bool:
    if not book.is_consistent or not book.bids or not book.asks:
        return False
    if any(
        not level.price.is_finite() or level.price <= 0 or level.quantity_lots <= 0
        for level in (*book.bids, *book.asks)
    ):
        return False
    if book.best_bid.price >= book.best_ask.price:
        return False
    if any(left.price <= right.price for left, right in pairwise(book.bids)) or any(
        left.price >= right.price for left, right in pairwise(book.asks)
    ):
        return False
    for candle in candles:
        prices = (candle.open, candle.high, candle.low, candle.close)
        if (
            any(not value.is_finite() or value <= 0 for value in prices)
            or candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
            or candle.volume < 0
        ):
            return False
    return True
