"""Exercise the Analytics HTTP boundary with a market-only upstream."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from market_analytics.app import create_app
from market_analytics.market_source import HttpMarketSource
from sentinel_contracts.analytics import MarketSourceSnapshot

NOW = datetime(2026, 9, 9, 9, tzinfo=UTC)
SOURCE = UUID("00000000-0000-0000-0000-000000000001")


def instrument(identifier="instrument", *, age_ms=0, valid=True):
    return {
        "instrument_id": identifier,
        "available": True,
        "market": {
            "instrument_id": identifier,
            "order_book": {
                "instrument_id": identifier,
                "bids": [{"price": "100", "quantity_lots": 2}],
                "asks": [{"price": "101" if valid else "99", "quantity_lots": 2}],
                "captured_at": NOW - timedelta(milliseconds=age_ms),
                "is_consistent": True,
            },
            "trading_status": {
                "instrument_id": identifier,
                "status": "NORMAL",
                "limit_order_available": True,
                "api_trade_available": True,
                "captured_at": NOW - timedelta(hours=1),
            },
        },
        "candles": [
            {
                "instrument_id": identifier,
                "open": str(100 + index),
                "high": str(102 + index),
                "low": str(99 + index),
                "close": str(101 + index),
                "volume": 10,
                "started_at": NOW - timedelta(minutes=20 - index),
                "is_complete": True,
                "captured_at": NOW,
            }
            for index in range(20)
        ],
    }


class Source:
    def __init__(self, instruments=None, *, captured_at=NOW, error=None):
        self.calls = []
        self.instruments = [instrument()] if instruments is None else instruments
        self.captured_at = captured_at
        self.error = error

    async def snapshot(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return MarketSourceSnapshot(
            snapshot_id="generation-1", captured_at=self.captured_at, instruments=self.instruments
        )


def request(ids=("instrument",), *, fallback="0.5"):
    return {
        "source_id": str(SOURCE),
        "instrument_ids": list(ids),
        "fallback": {
            "averaging_step_percent": fallback,
            "minimum_net_profit_percent": "0.5",
            "source": "STRATEGY",
        },
    }


def test_http_batch_computes_exact_metrics_from_one_market_only_request():
    source = Source([instrument("one"), instrument("two")])
    with TestClient(create_app(source, now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request(("one", "two")))
        assert client.get("/health").status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_id"] == "generation-1"
    assert payload["captured_at"] == "2026-09-09T09:00:00Z"
    assert payload["ttl_ms"] == 2000
    assert len(source.calls) == 1
    assert source.calls[0].model_dump(mode="json") == {"source_id": str(SOURCE), "instrument_ids": ["one", "two"]}
    for item in payload["instruments"]:
        assert item["freshness"] == "FRESH"
        assert item["metrics"]["mean_20"] == "110.5"
        assert item["metrics"]["mean_5"] == "118"
        assert item["metrics"]["range_low"] == "99"
        assert item["metrics"]["range_high"] == "121"


@pytest.mark.parametrize(
    ("elapsed", "expected"), [(0, "FRESH"), (1999, "FRESH"), (2000, "FRESH"), (2001, "STALE"), (-1, "STALE")]
)
def test_snapshot_deadline_is_checked_at_receipt(elapsed, expected):
    source = Source()
    with TestClient(create_app(source, now=lambda: NOW + timedelta(milliseconds=elapsed))) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    assert response.status_code == 200
    assert response.json()["instruments"][0]["freshness"] == expected


def test_stale_invalid_and_missing_instruments_do_not_poison_fresh_peer():
    source = Source([instrument("fresh"), instrument("stale", age_ms=3000), instrument("invalid", valid=False)])
    with TestClient(create_app(source, now=lambda: NOW)) as client:
        response = client.post(
            "/internal/v1/analytics/snapshots", json=request(("fresh", "stale", "invalid", "missing"))
        )
    assert response.status_code == 200
    assert {item["instrument_id"]: item["freshness"] for item in response.json()["instruments"]} == {
        "fresh": "FRESH",
        "stale": "STALE",
        "invalid": "UNAVAILABLE",
        "missing": "UNAVAILABLE",
    }


def test_changed_fallback_profile_keeps_market_generation_but_different_identity():
    source = Source([dict(instrument(), candles=[])])
    with TestClient(create_app(source, now=lambda: NOW)) as client:
        first = client.post("/internal/v1/analytics/snapshots", json=request()).json()
        second = client.post("/internal/v1/analytics/snapshots", json=request(fallback="0.7")).json()
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["profile_id"] != second["profile_id"]
    assert second["instruments"][0]["metrics"]["averaging_step_percent"] == "0.7"


def test_old_candle_history_blocks_buy_window_without_disabling_fresh_quotes():
    item = instrument()
    for candle in item["candles"]:
        candle["started_at"] -= timedelta(hours=1)
    with TestClient(create_app(Source([item]), now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    result = response.json()["instruments"][0]
    assert result["freshness"] == "FRESH"
    assert result["metrics"]["mean_20"] is None
    assert result["metrics"]["last_candle_at"] is None
    assert result["metrics"]["range_low"] is None
    assert result["metrics"]["source"] == "STRATEGY"


@pytest.mark.parametrize("error", [httpx.ReadTimeout("upstream fixture"), ValueError("invalid upstream fixture")])
def test_upstream_failure_returns_safe_unavailable_response(error):
    with TestClient(create_app(Source(error=error), now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    assert response.status_code == 503
    assert response.json() == {"detail": "MARKET_SOURCE_UNAVAILABLE"}


def test_market_http_adapter_sends_only_market_request():
    calls = []

    def handler(message):
        calls.append(message)
        return httpx.Response(
            200,
            json=MarketSourceSnapshot(
                snapshot_id="generation-1", captured_at=NOW, instruments=[instrument()]
            ).model_dump(mode="json"),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://core") as client:
            app = create_app(HttpMarketSource(client), now=lambda: NOW)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://analytics") as caller:
                return await caller.post("/internal/v1/analytics/snapshots", json=request())

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].url.path == "/internal/v1/market/snapshots"
    assert "authorization" not in calls[0].headers
    assert Decimal(response.json()["instruments"][0]["metrics"]["mean_20"]) == Decimal("110.5")


def test_rejected_request_does_not_echo_unexpected_data():
    source = Source()
    payload = request()
    payload["portfolio"] = {"test_marker": "MUST_NOT_ECHO_FIXTURE"}
    with TestClient(create_app(source, now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=payload)
    assert response.status_code == 422
    assert "MUST_NOT_ECHO_FIXTURE" not in response.text
    assert source.calls == []


def test_reobserved_identical_candle_does_not_destroy_confirmation_window():
    item = instrument()
    item["candles"].append({**item["candles"][-1], "captured_at": NOW - timedelta(milliseconds=100)})
    with TestClient(create_app(Source([item]), now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    assert response.json()["instruments"][0]["metrics"]["mean_20"] == "110.5"


def test_candle_readiness_is_stable_for_one_generation_at_age_boundary():
    item = instrument()
    for candle in item["candles"]:
        candle["started_at"] -= timedelta(minutes=2)
    clock = [NOW]
    with TestClient(create_app(Source([item]), now=lambda: clock[0])) as client:
        first = client.post("/internal/v1/analytics/snapshots", json=request()).json()
        clock[0] += timedelta(milliseconds=1)
        second = client.post("/internal/v1/analytics/snapshots", json=request()).json()
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["profile_id"] == second["profile_id"]
    assert first["instruments"][0]["freshness"] == second["instruments"][0]["freshness"] == "FRESH"
    assert first["instruments"][0]["metrics"]["mean_20"] == "110.5"
    assert first["instruments"][0]["metrics"] == second["instruments"][0]["metrics"]
