"""Pure safety validation for one atomic order-book snapshot."""

from datetime import datetime, timedelta
from itertools import pairwise

from sentinel_contracts.broker_execution import OrderBookSnapshot
from trading_automaton.domain.dtos import OrderBookValidationResult


class OrderBookValidationService:
    def validate(
        self,
        snapshot: OrderBookSnapshot,
        *,
        now: datetime,
        max_age: timedelta,
    ) -> OrderBookValidationResult:
        age_ms = max(0, int((now - snapshot.captured_at).total_seconds() * 1000))
        if not snapshot.bids or not snapshot.asks:
            return OrderBookValidationResult(False, "INVALID_ORDER_BOOK", age_ms)
        if any(
            not level.price.is_finite() or level.price <= 0 or level.quantity_lots <= 0
            for level in (*snapshot.bids, *snapshot.asks)
        ):
            return OrderBookValidationResult(False, "INVALID_ORDER_BOOK", age_ms)
        if any(left.price <= right.price for left, right in pairwise(snapshot.bids)) or any(
            left.price >= right.price for left, right in pairwise(snapshot.asks)
        ):
            return OrderBookValidationResult(False, "INVALID_ORDER_BOOK", age_ms)
        if snapshot.best_bid.price >= snapshot.best_ask.price:
            return OrderBookValidationResult(False, "CROSSED_ORDER_BOOK", age_ms)
        if now - snapshot.captured_at > max_age:
            return OrderBookValidationResult(False, "STALE_ORDER_BOOK", age_ms)
        if snapshot.captured_at > now:
            return OrderBookValidationResult(False, "FUTURE_ORDER_BOOK", age_ms)
        return OrderBookValidationResult(True, None, age_ms)
