"""Market-only JSON boundary rejects unknown data at every nesting level."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from sentinel_contracts.analytics import (
    AdaptiveThresholds,
    AnalyticsSnapshotRequest,
    MarketSnapshotRequest,
    MarketSourceSnapshot,
)

SOURCE = UUID("00000000-0000-0000-0000-000000000001")


def source_payload():
    return {
        "snapshot_id": "generation-1",
        "captured_at": "2026-09-09T12:00:00+03:00",
        "instruments": [
            {
                "instrument_id": "instrument",
                "available": True,
                "market": {
                    "instrument_id": "instrument",
                    "order_book": {
                        "instrument_id": "instrument",
                        "bids": [{"price": "100.000000001", "quantity_lots": 2}],
                        "asks": [{"price": "101", "quantity_lots": 2}],
                        "captured_at": "2026-09-09T09:00:00Z",
                        "is_consistent": True,
                    },
                },
                "candles": [],
            }
        ],
    }


def test_source_roundtrip_preserves_decimal_and_normalizes_utc():
    snapshot = MarketSourceSnapshot.model_validate(source_payload())
    assert snapshot.captured_at == datetime(2026, 9, 9, 9, tzinfo=UTC)
    assert snapshot.captured_at.tzinfo is UTC
    assert (
        json.loads(snapshot.model_dump_json())["instruments"][0]["market"]["order_book"]["bids"][0]["price"]
        == "100.000000001"
    )
    assert MarketSourceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize("level", ["request", "instrument", "market", "book", "level"])
def test_unknown_data_cannot_cross_market_boundary(level):
    payload = source_payload()
    if level == "request":
        with pytest.raises(ValidationError):
            MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("instrument",), account_id="fixture")
        return
    instrument = payload["instruments"][0]
    target = {
        "instrument": instrument,
        "market": instrument["market"],
        "book": instrument["market"]["order_book"],
        "level": instrument["market"]["order_book"]["bids"][0],
    }[level]
    target["unexpected"] = "fixture"
    with pytest.raises(ValidationError):
        MarketSourceSnapshot.model_validate(payload)


@pytest.mark.parametrize("ids", [(), ("",), ("i", "i"), tuple(str(i) for i in range(101))])
def test_request_bounds_and_unique_instrument_ids(ids):
    with pytest.raises(ValidationError):
        MarketSnapshotRequest(source_id=SOURCE, instrument_ids=ids)


def test_naive_nested_timestamp_is_rejected():
    payload = source_payload()
    payload["instruments"][0]["market"]["order_book"]["captured_at"] = "2026-09-09T09:00:00"
    with pytest.raises(ValidationError):
        MarketSourceSnapshot.model_validate(payload)


def test_fallback_profile_changes_identity_without_losing_decimal_precision():
    first = AnalyticsSnapshotRequest(
        SOURCE, ("instrument",), AdaptiveThresholds(Decimal("0.5"), Decimal("0.3"), "STRATEGY")
    )
    second = AnalyticsSnapshotRequest(
        SOURCE, ("instrument",), AdaptiveThresholds(Decimal("0.6"), Decimal("0.3"), "STRATEGY")
    )
    assert first.profile_id != second.profile_id
    assert json.loads(first.model_dump_json())["fallback"]["averaging_step_percent"] == "0.5"
