"""Worker versions are volatile observations, independent from readiness."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.services.runtime_versions import WorkerVersionStore
from sentinel_contracts.runtime_versions import RuntimeVersion
from sentinel_contracts.worker_diagnostics import CommandSyncDiagnostics, CommandSyncRow
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
                json={
                    "worker_id": "legacy",
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
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

    monkeypatch.setattr(
        "moex_sentinel.api.app.build_market_snapshot_gateway",
        lambda *args, **kwargs: MarketGateway(),
    )
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
                request.method,
                request.url.path,
                content=request.content,
                headers={"content-type": "application/json"},
            )
            return httpx.Response(result.status_code, content=result.content)

        with httpx.Client(base_url="http://core", transport=httpx.MockTransport(forward)) as http:
            worker = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
            worker.heartbeat("worker", datetime.now(UTC))
        payload = client.get("/api/diagnostics/status").json()
        assert payload["service_versions"]["worker"]["version"] == "5.6.7"
        assert payload["core"]["version"] == "0.2.0"
        assert payload["status"] == "UNKNOWN"


def test_worker_diagnostics_requires_identity_and_roundtrips_in_existing_heartbeat(
    tmp_path,
):
    now = datetime.now(UTC)
    payload = {
        "worker_id": "worker",
        "occurred_at": now.isoformat(),
        "worker_diagnostics": progress(completed=now, finished=now).model_dump(mode="json"),
    }
    with TestClient(
        create_app(
            test_auth_bypass=True,
            database_url=f"sqlite:///{tmp_path/'diagnostics-worker.db'}",
        )
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
        assert result["awaiting_observations"] == [
            "broker",
            "analytics",
            "market",
            "portfolio",
            "strategy",
        ]


def test_old_worker_diagnostics_payload_defaults_broker_runtime_to_unknown(tmp_path):
    now = datetime.now(UTC)
    payload = {
        "worker_id": "worker",
        "occurred_at": now.isoformat(),
        "runtime_version": {
            "version": "1.2.3",
            "instance_id": str(uuid4()),
            "environment": "TEST",
            "access_mode": "READ_ONLY",
        },
        "worker_diagnostics": {
            "completed_iterations": 1,
            "last_completed_at": now.isoformat(),
            "last_finished_at": now.isoformat(),
            "last_result": "COMPLETED",
            "error_code": None,
            "outbox": {"reason": "READ_FAILED"},
        },
    }
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'old-worker.db'}")) as client:
        response = client.post("/internal/automaton/heartbeats", json=payload)
        assert response.status_code == 200
        worker = client.get("/api/diagnostics/status").json()["worker"]

    assert worker["reason"] == "CONTROL_PROGRESS"
    assert worker["broker_runtime"]["status"] == "UNKNOWN"
    assert worker["broker_runtime"]["decision_eligibility"] == "UNKNOWN"
    assert worker["command_sync"] is None


def test_worker_command_sync_projection_flows_through_existing_heartbeat(tmp_path):
    now = datetime.now(UTC)
    projection = CommandSyncDiagnostics(
        observation="OBSERVED",
        reason="OBSERVED",
        broker_id="00000000-0000-4000-8000-000000000001",
        captured_at=now,
        complete=True,
        cached_count=1,
        claimed_count=0,
        monitored_count=1,
        grouped_count=1,
        publication_observation="OBSERVED",
        rows=(
            CommandSyncRow(
                automation_id="00000000-0000-4000-8000-000000000501",
                state="IN_WORK",
                revision=2,
                last_sequence_number=4,
                binding_complete=True,
                bootstrap=False,
                active_intent_state=None,
                active_intent_terminal_present=None,
                pending_fact_count=0,
                failed_fact_count=0,
                monitored=True,
                grouped=True,
            ),
        ),
    )
    diagnostics = progress(completed=now, finished=now).model_copy(update={"command_sync": projection})
    with TestClient(
        create_app(
            test_auth_bypass=True,
            database_url=f"sqlite:///{tmp_path/'command-sync.db'}",
        )
    ) as client:

        def forward(request):
            result = client.request(
                request.method,
                request.url.path,
                content=request.content,
                headers={"content-type": "application/json"},
            )
            return httpx.Response(result.status_code, content=result.content)

        with httpx.Client(base_url="http://core", transport=httpx.MockTransport(forward)) as http:
            worker = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
            worker.heartbeat("worker", now, diagnostics)

        observed = client.get("/api/diagnostics/status").json()["worker"]["command_sync"]

    assert observed["observation"] == "OBSERVED"
    assert observed["complete"] is True
    assert observed["rows"][0]["automation_id"] == "00000000-0000-4000-8000-000000000501"


def test_core_does_not_refresh_command_projection_age_with_repeated_heartbeats():
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    clock = [now]
    ticks = [0.0]
    projection = CommandSyncDiagnostics(
        observation="OBSERVED",
        reason="OBSERVED",
        broker_id="00000000-0000-4000-8000-000000000001",
        captured_at=now,
        complete=True,
        cached_count=0,
        claimed_count=0,
        monitored_count=0,
        grouped_count=0,
        publication_observation="OBSERVED",
    )
    diagnostics = progress(completed=now, finished=now).model_copy(update={"command_sync": projection})
    identity = RuntimeVersion(
        version="1.2.3",
        instance_id=uuid4(),
        environment="TEST",
        access_mode="READ_ONLY",
    )
    store = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
    store.observe(identity, now, diagnostics)
    clock[0] += timedelta(seconds=20)
    ticks[0] = 20
    store.observe(identity, clock[0], diagnostics)
    clock[0] += timedelta(seconds=9, milliseconds=999)
    ticks[0] = 29.999
    assert store.diagnostics_snapshot()[1].command_sync is not None
    clock[0] += timedelta(milliseconds=1)
    ticks[0] = 30
    assert store.diagnostics_snapshot()[1].command_sync is None
