from pathlib import Path

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base


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

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.post("/api/brokers", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["fields"] == [{"path": "display_name", "code": "REQUIRED", "message": "Укажите название брокера."}]
    assert "synthetic-token" not in response.text
