from importlib import import_module
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base

BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
ORIGIN_RUNTIME_ID = UUID("85155424-74ac-4d87-b13c-1410e9020be9")
INTENT_ID = UUID("80e66106-a1a2-418a-895d-de74a81a754c")
WORKER_TOKEN = "a" * 64
RUNTIME_SECRET = "b" * 64


@pytest.fixture
def worker_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-prod-operator")
    monkeypatch.setenv("INTERNAL_WORKER_ID", "worker-prod-1")
    monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(BROKER_ID))
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    create_app = import_module("moex_sentinel.api.app").create_app

    database_url = f"sqlite:///{tmp_path / 'submit-attempt.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_brokers "
                "(id, api_slug, display_name, environment, fqdn, settings, external_account_id, state, "
                "created_at, updated_at) "
                "VALUES (:id, 't_invest', 'Synthetic', 'PROD', 'invest-public-api.tbank.ru:443', '{}', "
                "'account-1', 'ACTIVE', '2026-10-09T00:00:00Z', '2026-10-09T00:00:00Z')"
            ),
            {"id": str(BROKER_ID)},
        )
    engine.dispose()

    app = create_app(database_url=database_url, test_auth_bypass=True)
    headers = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "X-Runtime-Instance-ID": str(RUNTIME_ID),
        "X-Runtime-Secret": RUNTIME_SECRET,
    }
    with TestClient(app) as client:
        registration = client.post(
            "/internal/automaton/runtime-instances",
            json={
                "worker_id": "worker-prod-1",
                "runtime_instance_id": str(RUNTIME_ID),
                "broker_id": str(BROKER_ID),
                "application_environment": "PROD",
                "access_mode": "READ_ONLY",
                "strategy_enabled": False,
            },
            headers=headers,
        )
        assert registration.status_code == 200
        assert registration.json()["registration_status"] == "OWNER"
        yield client, headers


def test_submit_attempt_accepts_worker_origin_query_and_returns_typed_view(worker_client) -> None:
    client, headers = worker_client
    service = client.app.state.usecases.trading_admission
    submit_attempt = Mock(wraps=service.submit_attempt)
    service.submit_attempt = submit_attempt

    response = client.get(
        f"/internal/automaton/brokers/{BROKER_ID}/submit-attempts/{INTENT_ID}",
        params={
            "original_runtime_instance_id": str(ORIGIN_RUNTIME_ID),
            "original_generation": "5",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "broker_id": str(BROKER_ID),
        "account_id": "account-1",
        "idempotency_key": str(INTENT_ID),
        "consumed_state": None,
        "proven_not_granted": False,
        "original_runtime_instance_id": None,
        "original_generation": None,
        "original_generation_retired": False,
        "absence_basis": None,
    }
    submit_attempt.assert_called_once_with(
        str(BROKER_ID),
        str(INTENT_ID),
        original_runtime_instance_id=ORIGIN_RUNTIME_ID,
        generation=5,
    )
    assert isinstance(submit_attempt.call_args.kwargs["original_runtime_instance_id"], UUID)
    assert type(submit_attempt.call_args.kwargs["generation"]) is int


def test_submit_attempt_accepts_missing_origin_pair(worker_client) -> None:
    client, headers = worker_client
    service = client.app.state.usecases.trading_admission
    submit_attempt = Mock(wraps=service.submit_attempt)
    service.submit_attempt = submit_attempt

    response = client.get(f"/internal/automaton/brokers/{BROKER_ID}/submit-attempts/{INTENT_ID}", headers=headers)

    assert response.status_code == 200
    assert response.json()["idempotency_key"] == str(INTENT_ID)
    submit_attempt.assert_called_once_with(
        str(BROKER_ID),
        str(INTENT_ID),
        original_runtime_instance_id=None,
        generation=None,
    )


@pytest.mark.parametrize(
    "params",
    [
        {"original_runtime_instance_id": "not-a-uuid", "original_generation": "5"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "0"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "-5"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "five"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "5.0"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "true"},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID), "original_generation": "9" * 5000},
        {"original_runtime_instance_id": str(ORIGIN_RUNTIME_ID)},
        {"original_generation": "5"},
    ],
)
def test_submit_attempt_rejects_malformed_or_incomplete_origin_without_calling_service(worker_client, params) -> None:
    client, headers = worker_client
    service = client.app.state.usecases.trading_admission
    original = service.submit_attempt
    called = False

    def record_call(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    service.submit_attempt = record_call
    response = client.get(
        f"/internal/automaton/brokers/{BROKER_ID}/submit-attempts/{INTENT_ID}",
        params=params,
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]
    assert not called


def test_submit_attempt_rejects_wrong_broker_scope_without_calling_service(worker_client) -> None:
    client, headers = worker_client
    service = client.app.state.usecases.trading_admission
    original = service.submit_attempt
    called = False

    def record_call(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    service.submit_attempt = record_call
    response = client.get(
        f"/internal/automaton/brokers/{UUID(int=9)}/submit-attempts/{INTENT_ID}",
        params={
            "original_runtime_instance_id": str(ORIGIN_RUNTIME_ID),
            "original_generation": "5",
        },
        headers=headers,
    )

    assert response.status_code == 404
    assert not called
