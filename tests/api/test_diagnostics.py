"""Protected Core diagnostics use only local readiness observations."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import hash_password


def test_diagnostics_requires_authentication_and_returns_uncached_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("diagnostics test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
    application = create_app(database_url=f"sqlite:///{tmp_path / 'diagnostics-auth.db'}")
    with TestClient(application, base_url="http://127.0.0.1:8000") as client:
        anonymous = client.get("/api/diagnostics/status")
        assert anonymous.status_code == 401
        assert anonymous.headers["Cache-Control"] == "no-store"
        login = client.post("/api/auth/login", json={"username": "operator", "password": "diagnostics test password"})
        assert login.status_code == 200
        response = client.get("/api/diagnostics/status")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["status"] == "UNKNOWN"
        assert "diagnostics test password" not in response.text


@pytest.mark.parametrize(
    ("database_ok", "schema_ok", "status", "core_status", "database", "schema", "reason"),
    [
        (True, True, "UNKNOWN", "OK", "ok", "compatible", "READY"),
        (False, True, "DOWN", "DOWN", "error", "unknown", "DATABASE_UNAVAILABLE"),
        (True, False, "DEGRADED", "DEGRADED", "ok", "not_ready", "SCHEMA_NOT_READY"),
    ],
)
def test_diagnostics_readiness_contract(
    tmp_path: Path, database_ok, schema_ok, status, core_status, database, schema, reason
) -> None:
    schema_calls = []

    def probe_schema(_engine: Engine) -> bool:
        schema_calls.append("schema")
        return schema_ok

    application = create_app(
        test_auth_bypass=True,
        database_url=f"sqlite:///{tmp_path / 'diagnostics.db'}",
        database_checker=lambda _engine: database_ok,
        schema_checker=probe_schema,
    )
    with TestClient(application) as client:
        started = datetime.now(UTC)
        response = client.get("/api/diagnostics/status")
        finished = datetime.now(UTC)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    captured = payload.pop("captured_at")
    assert captured.endswith("Z")
    assert len(captured.rsplit(".", 1)[1]) == 4
    timestamp = datetime.fromisoformat(captured)
    assert started.replace(microsecond=started.microsecond // 1000 * 1000) <= timestamp <= finished
    assert payload.pop("service_versions") == {
        "worker": {
            "observation": "UNKNOWN",
            "version": None,
            "received_at": None,
            "age_ms": None,
            "reason": "NOT_OBSERVED",
        },
        "analytics": {
            "observation": "UNKNOWN",
            "version": None,
            "received_at": None,
            "age_ms": None,
            "reason": "NOT_CONFIGURED",
        },
    }
    worker = payload.pop("worker")
    assert worker["status"] == "UNKNOWN"
    assert worker["reason"] == "NOT_OBSERVED"
    assert worker["completed_iterations"] is None
    assert worker["outbox"]["pending_count"] is None
    assert payload == {
        "status": status,
        "core": {"status": core_status, "version": "0.2.0", "database": database, "schema": schema, "reason": reason},
        "runtime": {"environment": "TEST", "access_mode": "READ_ONLY"},
        "awaiting_observations": ["worker", "broker", "analytics", "market", "outbox", "portfolio", "strategy"],
    }
    assert schema_calls == (["schema"] if database_ok else [])


@pytest.mark.parametrize("failed_probe", ["database", "schema"])
def test_diagnostics_suppresses_probe_secrets(tmp_path: Path, failed_probe: str) -> None:
    def unavailable(_engine: Engine) -> bool:
        raise RuntimeError("postgresql://secret:password@host/account-123")

    application = create_app(
        test_auth_bypass=True,
        database_url=f"sqlite:///{tmp_path / 'diagnostics-error.db'}",
        **{f"{failed_probe}_checker": unavailable},
    )
    with TestClient(application) as client:
        response = client.get("/api/diagnostics/status")
        health = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == ("DOWN" if failed_probe == "database" else "DEGRADED")
    assert health.status_code == 503
    for sensitive in ("secret", "password", "host", "account-123", "postgresql"):
        assert sensitive not in response.text


def test_diagnostics_uses_settings_captured_by_create_app(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'diagnostics-runtime.db'}")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    with TestClient(application) as client:
        response = client.get("/api/diagnostics/status")
    assert response.status_code == 200
    assert response.json()["runtime"] == {"environment": "TEST", "access_mode": "READ_ONLY"}


def test_diagnostics_probe_runs_outside_event_loop_without_market_or_broker_calls(tmp_path: Path, monkeypatch) -> None:
    loop_threads = []
    probe_threads = []

    class UnusedMarketGateway:
        async def close(self) -> None:
            loop_threads.append(threading.get_ident())

        async def snapshot(self, _request):
            pytest.fail("Diagnostics must not request broker or market observations")

    monkeypatch.setattr(
        "moex_sentinel.api.app.build_market_snapshot_gateway", lambda *args, **kwargs: UnusedMarketGateway()
    )

    def probe(_engine: Engine) -> bool:
        probe_threads.append(threading.get_ident())
        return True

    application = create_app(
        test_auth_bypass=True,
        database_url=f"sqlite:///{tmp_path / 'diagnostics-thread.db'}",
        database_checker=probe,
    )
    with TestClient(application) as client:
        response = client.get("/api/diagnostics/status")
    assert response.status_code == 200
    assert len(probe_threads) == 1
    assert probe_threads[0] != loop_threads[0]
