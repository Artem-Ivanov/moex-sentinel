from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel_contracts.broker_execution import OrderBookLevel, OrderBookSnapshot
from trading_automaton.services.order_book_validation import OrderBookValidationService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def book(
    bid: Decimal = Decimal("99.9"),
    ask: Decimal = Decimal("100.1"),
    *,
    captured_at: datetime = NOW,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        (OrderBookLevel(bid, 1),),
        (OrderBookLevel(ask, 1),),
        captured_at,
    )


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (OrderBookSnapshot((), (), NOW), "INVALID_ORDER_BOOK"),
        (book(Decimal(), Decimal("100")), "INVALID_ORDER_BOOK"),
        (book(Decimal("100"), Decimal()), "INVALID_ORDER_BOOK"),
        (book(Decimal("100"), Decimal("100")), "CROSSED_ORDER_BOOK"),
        (book(Decimal("100.1"), Decimal("100")), "CROSSED_ORDER_BOOK"),
        (book(captured_at=NOW - timedelta(seconds=3)), "STALE_ORDER_BOOK"),
    ],
    ids=["empty-depth", "zero-bid", "zero-ask", "locked-book", "crossed-book", "stale-by-one-second"],
)
def test_rejects_order_book_that_cannot_safely_drive_a_trade(snapshot, reason) -> None:
    result = OrderBookValidationService().validate(
        snapshot,
        now=NOW,
        max_age=timedelta(seconds=2),
    )

    assert result.valid is False
    assert result.reason_code == reason


def test_accepts_positive_non_crossed_fresh_order_book() -> None:
    result = OrderBookValidationService().validate(
        book(),
        now=NOW,
        max_age=timedelta(seconds=2),
    )

    assert result.valid is True
    assert result.reason_code is None
    assert result.age_ms == 0


@pytest.mark.parametrize(
    "snapshot",
    [
        book().model_copy(update={"bids": (OrderBookLevel(Decimal("99"), 0),)}),
        book().model_copy(update={"asks": (OrderBookLevel(Decimal("101"), -1),)}),
        book().model_copy(update={"bids": (book().best_bid.model_copy(update={"price": Decimal("NaN")}),)}),
        book().model_copy(update={"asks": (book().best_ask.model_copy(update={"price": Decimal("Infinity")}),)}),
        book().model_copy(update={"bids": (OrderBookLevel(Decimal("99"), 1), OrderBookLevel(Decimal("100"), 1))}),
        book().model_copy(update={"asks": (OrderBookLevel(Decimal("101"), 1), OrderBookLevel(Decimal("100"), 1))}),
        book().model_copy(update={"bids": (OrderBookLevel(Decimal("99"), 1), OrderBookLevel(Decimal("99"), 1))}),
    ],
    ids=[
        "zero-bid-volume",
        "negative-ask-volume",
        "nan-bid",
        "infinite-ask",
        "ascending-bids",
        "descending-asks",
        "duplicate-bid-price",
    ],
)
def test_rejects_invalid_depth_and_nonfinite_prices(snapshot) -> None:
    result = OrderBookValidationService().validate(snapshot, now=NOW, max_age=timedelta(seconds=2))
    assert result.valid is False
    assert result.reason_code == "INVALID_ORDER_BOOK"


@pytest.mark.parametrize(
    ("age", "valid", "reason"),
    [
        (timedelta(seconds=2), True, None),
        (timedelta(seconds=2, milliseconds=1), False, "STALE_ORDER_BOOK"),
        (timedelta(milliseconds=-1), False, "FUTURE_ORDER_BOOK"),
    ],
    ids=["freshness-exactly-2s", "stale-by-1ms", "future-by-1ms"],
)
def test_order_book_time_boundaries(age, valid, reason) -> None:
    result = OrderBookValidationService().validate(book(captured_at=NOW - age), now=NOW, max_age=timedelta(seconds=2))
    assert result.valid is valid
    assert result.reason_code == reason
