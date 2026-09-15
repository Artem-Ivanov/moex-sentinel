"""Internal market endpoint stays credential-free and closes source ownership."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from sentinel_contracts.analytics import MarketSourceSnapshot


class Gateway:
    def __init__(self):
        self.closed = False
        self.requests = []

    async def snapshot(self, request):
        self.requests.append(request)
        return MarketSourceSnapshot(
            snapshot_id="synthetic-generation",
            captured_at=datetime(2026, 9, 9, tzinfo=UTC),
            ttl_ms=2000,
            instruments=(),
        )

    async def close(self):
        self.closed = True


def test_market_route_owns_lifecycle_and_rejects_execution_fields(monkeypatch, tmp_path):
    gateway = Gateway()
    retry_limits = []

    def build_gateway(_, *, retry_limit):
        retry_limits.append(retry_limit)
        return gateway

    monkeypatch.setenv("SANDBOX_RETRY_LIMIT", "2")
    monkeypatch.setattr("moex_sentinel.api.app.build_market_snapshot_gateway", build_gateway, raising=False)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'market-gateway.db'}")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert gateway.requests == []
        response = client.post(
            "/internal/v1/market/snapshots",
            json={"source_id": "00000000-0000-0000-0000-000000000001", "instrument_ids": ["AAA"]},
        )
        assert response.status_code == 200
        assert response.json()["snapshot_id"] == "synthetic-generation"
        assert "token" not in response.text
        invalid = client.post(
            "/internal/v1/market/snapshots",
            json={
                "source_id": "00000000-0000-0000-0000-000000000001",
                "instrument_ids": ["AAA"],
                "account_id": "synthetic-account",
            },
        )
        assert invalid.status_code == 422
    assert gateway.closed
    assert retry_limits == [2]


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        pytest.param(LookupError("private source"), 404, "Market source not found.", id="missing-source"),
        pytest.param(ValueError("private configuration"), 409, "Market source unavailable.", id="unavailable-source"),
    ],
)
def test_market_source_errors_preserve_string_detail_contract(monkeypatch, error, status_code, detail):
    class UnavailableGateway(Gateway):
        async def snapshot(self, request):
            raise error

    gateway = UnavailableGateway()
    monkeypatch.setattr("moex_sentinel.api.app.build_market_snapshot_gateway", lambda *args, **kwargs: gateway)
    with TestClient(create_app(database_url="sqlite:///:memory:")) as client:
        response = client.post(
            "/internal/v1/market/snapshots",
            json={"source_id": "00000000-0000-0000-0000-000000000001", "instrument_ids": ["AAA"]},
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_unexpected_market_failure_uses_server_error_without_private_details(monkeypatch):
    class BrokenGateway(Gateway):
        async def snapshot(self, request):
            raise RuntimeError("private connector detail")

    monkeypatch.setattr("moex_sentinel.api.app.build_market_snapshot_gateway", lambda *args, **kwargs: BrokenGateway())
    with TestClient(create_app(database_url="sqlite:///:memory:"), raise_server_exceptions=False) as client:
        response = client.post(
            "/internal/v1/market/snapshots",
            json={"source_id": "00000000-0000-0000-0000-000000000001", "instrument_ids": ["AAA"]},
        )

    assert response.status_code == 500
    assert "private connector detail" not in response.text
