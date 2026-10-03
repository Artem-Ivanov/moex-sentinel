from pathlib import Path

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


def test_broker_api_returns_safe_field_errors(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'brokers.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    payload = {
        "display_name": "",
        "provider_code": "TINVEST",
        "environment_code": "SANDBOX",
        "adapter_code": "TINVEST_SANDBOX",
        "enabled": True,
        "is_test": True,
        "account_id": None,
        "fields": [
            {"name": "token", "value": "synthetic-token"},
            {"name": "fqdn", "value": "sandbox-invest-public-api.tbank.ru:443"},
        ],
    }

    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
        response = client.post("/api/brokers", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["fields"] == [{"path": "display_name", "code": "REQUIRED", "message": "Укажите название брокера."}]
    assert "synthetic-token" not in response.text


def test_delete_hides_broker_after_reload_and_preserves_disabled_settings(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'broker-delete.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    repository = UserBrokerRepository(create_session_factory(engine))
    payload = {
        "display_name": "Delete me",
        "provider_code": "TINVEST",
        "environment_code": "SANDBOX",
        "adapter_code": "TINVEST_SANDBOX",
        "enabled": True,
        "is_test": True,
        "account_id": "account-to-archive",
        "fields": [
            {"name": "token", "value": "synthetic-token"},
            {"name": "fqdn", "value": "sandbox-invest-public-api.tbank.ru:443"},
        ],
    }
    try:
        with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
            created = client.post("/api/brokers", json=payload)
            assert created.status_code == 201
            broker_id = created.json()["id"]
            disabled = client.post(
                "/api/brokers", json={**payload, "enabled": False, "account_id": "ordinary-disabled"}
            )
            assert disabled.status_code == 201
            disabled_id = disabled.json()["id"]
            assert client.delete(f"/api/brokers/{broker_id}").status_code == 204
            assert {item["id"] for item in client.get("/api/brokers").json()["brokers"]} == {disabled_id}
            assert client.delete(f"/api/brokers/{broker_id}").status_code == 204
            assert client.put(f"/api/brokers/{broker_id}", json=payload).status_code == 404
            assert client.post("/api/brokers", json=payload).status_code == 409

        with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as reopened:
            settings = reopened.get("/api/brokers").json()["brokers"]
            assert [item["id"] for item in settings] == [disabled_id]
            assert settings[0]["enabled"] is False
            assert (
                reopened.put(
                    f"/api/brokers/{disabled_id}", json={**payload, "account_id": "ordinary-disabled"}
                ).status_code
                == 200
            )
        archived = repository.get(broker_id)
        assert archived.archived_at is not None
        assert archived.external_account_id == payload["account_id"]
        assert archived.settings == {"token": "synthetic-token"}
        assert archived.enabled is False
    finally:
        engine.dispose()
