"""Worker versions are volatile observations, independent from readiness."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from tests.services.test_runtime_versions import progress
from trading_automaton.adapters.core_client import CoreClient


def test_worker_heartbeat_preserves_ack_and_exposes_actual_runtime_version(tmp_path):
    app = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'versions.db'}")
    with TestClient(app) as client:
        before = client.get("/api/diagnostics/status").json()
        assert "service_versions" in before
        assert before["service_versions"]["worker"]["reason"] == "NOT_OBSERVED"
        now = datetime.now(UTC).isoformat()
        payload = {
            "worker_id": "worker",
            "occurred_at": now,
            "runtime_version": {
                "version": "7.8.9",
                "instance_id": str(uuid4()),
                "environment": "TEST",
                "access_mode": "READ_ONLY",
            },
        }
        response = client.post("/internal/automaton/heartbeats", json=payload)
        assert response.status_code == 200
        assert set(response.json()) == {"worker_id", "occurred_at"}
        observed = client.get("/api/diagnostics/status").json()
        assert observed["service_versions"]["worker"]["version"] == "7.8.9"
        assert observed["service_versions"]["worker"]["observation"] == "OBSERVED"
        assert observed["status"] == "UNKNOWN"
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'versions.db'}")) as client:
        assert client.get("/api/diagnostics/status").json()["service_versions"]["worker"]["version"] is None


def test_legacy_heartbeat_does_not_create_runtime_observation(tmp_path):
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'legacy.db'}")) as client:
        assert (
            client.post(
                "/internal/automaton/heartbeats",
                json={"worker_id": "legacy", "occurred_at": datetime.now(UTC).isoformat()},
            ).status_code
            == 200
        )
        assert client.get("/api/diagnostics/status").json()["service_versions"]["worker"]["reason"] == "NOT_OBSERVED"


def test_market_gateway_is_closed_even_when_analytics_client_close_fails(tmp_path, monkeypatch):

    closed = []

    class MarketGateway:
        async def close(self):
            closed.append("market")

    async def failed_close(_self):
        raise RuntimeError("analytics cleanup failed")

    monkeypatch.setattr("moex_sentinel.api.app.build_market_snapshot_gateway", lambda *args, **kwargs: MarketGateway())
    monkeypatch.setattr(httpx.AsyncClient, "aclose", failed_close)
    with (
        pytest.raises(RuntimeError, match="analytics cleanup failed"),
        TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'cleanup.db'}")) as client,
    ):
        assert client.get("/api/diagnostics/status").status_code == 200
    assert closed == ["market"]


def test_actual_worker_client_to_core_diagnostics_preserves_distinct_version(tmp_path, monkeypatch):

    monkeypatch.setattr("trading_automaton.adapters.core_client.SERVICE_VERSION", "5.6.7")
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'worker-core.db'}")) as client:

        def forward(request):
            result = client.request(
                request.method, request.url.path, content=request.content, headers={"content-type": "application/json"}
            )
            return httpx.Response(result.status_code, content=result.content)

        with httpx.Client(base_url="http://core", transport=httpx.MockTransport(forward)) as http:
            worker = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
            worker.heartbeat("worker", datetime.now(UTC))
        payload = client.get("/api/diagnostics/status").json()
        assert payload["service_versions"]["worker"]["version"] == "5.6.7"
        assert payload["core"]["version"] == "0.2.0"
        assert payload["status"] == "UNKNOWN"


def test_worker_diagnostics_requires_identity_and_roundtrips_in_existing_heartbeat(tmp_path):
    now = datetime.now(UTC)
    payload = {
        "worker_id": "worker",
        "occurred_at": now.isoformat(),
        "worker_diagnostics": progress(completed=now, finished=now).model_dump(mode="json"),
    }
    with TestClient(
        create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'diagnostics-worker.db'}")
    ) as client:
        assert client.post("/internal/automaton/heartbeats", json=payload).status_code == 422
        payload["runtime_version"] = {
            "version": "1.2.3",
            "instance_id": str(uuid4()),
            "environment": "TEST",
            "access_mode": "READ_ONLY",
        }
        response = client.post("/internal/automaton/heartbeats", json=payload)
        assert response.status_code == 200
        assert set(response.json()) == {"worker_id", "occurred_at"}
        result = client.get("/api/diagnostics/status").json()
        assert result["worker"]["reason"] == "CONTROL_PROGRESS"
        assert result["worker"]["outbox"]["reason"] == "CLEAR"
        assert result["status"] == "UNKNOWN"
        assert result["awaiting_observations"] == ["broker", "analytics", "market", "portfolio", "strategy"]
