"""Exercise the Analytics HTTP boundary with a market-only upstream."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from market_analytics.app import create_app
from market_analytics.market_source import HttpMarketSource
from sentinel_contracts.analytics import MarketSourceSnapshot
from tests.market_analytics.market_source_helpers import NOW, Source, instrument

SOURCE = UUID("00000000-0000-0000-0000-000000000001")


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
    ("elapsed", "expected"),
    [(0, "FRESH"), (1999, "FRESH"), (2000, "FRESH"), (2001, "STALE"), (-1, "STALE")],
    ids=["fresh-now", "fresh-before-deadline", "freshness-exactly-2s", "stale-by-1ms", "future-by-1ms"],
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


@pytest.mark.parametrize(
    "upstream_result",
    [
        pytest.param(httpx.ReadTimeout("MUST_NOT_ECHO_UPSTREAM"), id="timeout"),
        pytest.param(httpx.Response(503, text="MUST_NOT_ECHO_UPSTREAM"), id="non-success-status"),
        pytest.param(httpx.Response(200, content=b"MUST_NOT_ECHO_UPSTREAM"), id="malformed-market-json"),
    ],
)
def test_upstream_failure_returns_safe_unavailable_response(upstream_result):
    """Exercise unavailable mapping through the actual HTTP market adapter."""

    def handler(_message):
        if isinstance(upstream_result, Exception):
            raise upstream_result
        return upstream_result

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://core") as client:
            app = create_app(HttpMarketSource(client), now=lambda: NOW)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://analytics") as caller:
                return await caller.post("/internal/v1/analytics/snapshots", json=request())

    response = asyncio.run(scenario())
    assert response.status_code == 503
    assert response.json() == {"detail": "MARKET_SOURCE_UNAVAILABLE"}


def test_unrequested_upstream_instrument_returns_safe_unavailable_response():
    """The actual calculator rejects a source instrument outside the requested batch."""
    with TestClient(create_app(Source([instrument("unexpected")]), now=lambda: NOW)) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    assert response.status_code == 503
    assert response.json() == {"detail": "MARKET_SOURCE_UNAVAILABLE"}


def test_unexpected_source_error_remains_safe_internal_server_error():
    """Unexpected source defects are not classified as expected market unavailability."""
    source = Source(error=RuntimeError("MUST_NOT_ECHO_UPSTREAM"))
    with TestClient(create_app(source, now=lambda: NOW), raise_server_exceptions=False) as client:
        response = client.post("/internal/v1/analytics/snapshots", json=request())
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert len(source.calls) == 1


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
