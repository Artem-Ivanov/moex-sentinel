from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from moex_sentinel.api.app import create_app


def test_health_reports_available_database(tmp_path: Path) -> None:
    application = create_app(database_url=f"sqlite:///{tmp_path / 'health.db'}")

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    UUID(response.headers["X-Process-ID"])
    assert response.json() == {
        "status": "ok",
        "service": "backend",
        "version": "0.1.0",
        "database": "ok",
        "schema": "compatible",
    }


def test_health_reports_database_failure_without_exposing_error() -> None:
    def unavailable(_engine: Engine) -> bool:
        raise RuntimeError("sensitive connection detail")

    application = create_app(
        database_url="sqlite:///:memory:",
        database_checker=unavailable,
    )

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "service": "backend",
        "version": "0.1.0",
        "database": "error",
        "schema": "incompatible",
    }
    assert "sensitive" not in response.text


def test_health_reports_schema_incompatibility_separately_without_exposing_revision() -> None:
    def incompatible(_engine: Engine) -> bool:
        raise RuntimeError("synthetic-actual-revision")

    application = create_app(
        database_url="sqlite:///:memory:",
        schema_checker=incompatible,
    )

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "service": "backend",
        "version": "0.1.0",
        "database": "ok",
        "schema": "incompatible",
    }
    assert "synthetic-actual-revision" not in response.text


def test_http_process_id_is_inherited_when_header_is_valid(tmp_path: Path) -> None:
    process_id = "9c8a54fd-0000-4000-8000-000000000001"
    application = create_app(database_url=f"sqlite:///{tmp_path / 'health-context.db'}")

    with TestClient(application) as client:
        response = client.get("/api/health", headers={"X-Process-ID": process_id})

    assert response.headers["X-Process-ID"] == process_id
