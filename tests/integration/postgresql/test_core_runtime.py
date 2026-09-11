"""Core readiness acceptance against the real PostgreSQL runtime."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.database import create_database_engine


@pytest.mark.postgresql
def test_core_health_accepts_current_postgresql_schema(isolated_postgresql_database_url: URL) -> None:
    application = create_app(database_url=isolated_postgresql_database_url)

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.json()["schema"] == "compatible"


@pytest.mark.postgresql
def test_core_health_rejects_postgresql_without_current_revision(
    isolated_postgresql_database_url: URL,
) -> None:
    engine = create_database_engine(isolated_postgresql_database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
    engine.dispose()
    application = create_app(database_url=isolated_postgresql_database_url)

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
    assert "a0f211000001" not in response.text
