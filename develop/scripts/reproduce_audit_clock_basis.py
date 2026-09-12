"""Offline reproduction of two clock bases; no API, DB or trading calls."""
# Offline assertion-based diagnostic with JSON output.
# ruff: noqa: T201, S101

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sentinel_contracts.broker_execution import OrderBookLevel, OrderBookSnapshot
from trading_automaton.services.order_book_validation import OrderBookValidationService

frame = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
book_at = frame + timedelta(milliseconds=1)
wall_now = frame + timedelta(milliseconds=100)
book = OrderBookSnapshot((OrderBookLevel(Decimal("100"), 1),), (OrderBookLevel(Decimal("101"), 1),), book_at)
validator = OrderBookValidationService()
results = {}
for name, now in [("worker_admission_wall_clock", wall_now), ("scheduler_frame_clock", frame)]:
    result = validator.validate(book, now=now, max_age=timedelta(seconds=2))
    results[name] = {
        "valid": result.valid,
        "reason_code": result.reason_code,
        "signed_age_ms": (now - book_at).total_seconds() * 1000,
    }
print(
    json.dumps(
        {
            "synthetic": True,
            "frame_at": frame.isoformat(),
            "book_at": book_at.isoformat(),
            "wall_now": wall_now.isoformat(),
            "results": results,
        },
        indent=2,
    )
)
assert results["worker_admission_wall_clock"]["valid"]
assert results["scheduler_frame_clock"]["reason_code"] == "FUTURE_ORDER_BOOK"
