"""Worker identity observes the same captured server contour as the public API."""

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.user_brokers import UserBrokerDraft, UserBrokerState
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


def test_internal_runtime_matches_captured_public_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    with TestClient(application) as client:
        response = client.get("/internal/runtime")
        assert response.status_code == 200
        assert (
            response.json() == client.get("/api/runtime").json() == {"environment": "TEST", "access_mode": "READ_ONLY"}
        )


@pytest.mark.parametrize("state", [UserBrokerState.ACTIVE, UserBrokerState.DISABLED])
def test_broker_scope_is_readable_without_credentials(monkeypatch, tmp_path, state):
    database_url = f"sqlite:///{tmp_path / 'scope.db'}"
    engine = create_database_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        repository = UserBrokerRepository(create_session_factory(engine))
        record = repository.create(
            UserBrokerDraft(
                "t_invest",
                "Disabled",
                "TEST",
                "sandbox-invest-public-api.tbank.ru:443",
                {},
                "account-1",
                state,
            )
        )
        with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
            response = client.get(f"/internal/automaton/brokers/{record.id}/scope")
        assert response.status_code == 200
        assert response.json() == {"broker_id": record.id, "environment": "TEST", "account_id": "account-1"}
    finally:
        engine.dispose()
