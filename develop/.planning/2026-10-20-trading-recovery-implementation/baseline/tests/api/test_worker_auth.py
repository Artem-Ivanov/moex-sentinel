from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.api.worker_auth import WorkerAuthenticationError, WorkerAuthenticator
from moex_sentinel.config import Settings
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base

BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
WORKER_TOKEN = "a" * 64
RUNTIME_SECRET = "b" * 64


@dataclass(frozen=True)
class RuntimeRecord:
    runtime_instance_id: str
    user_broker_id: str
    worker_id: str
    environment: str
    access_mode: str
    strategy_enabled: bool
    secret_digest: str
    registration_status: str
    generation: int | None
    lease_expires_at: object | None
    recovery_only: bool = False


def settings() -> Settings:
    return Settings(
        _env_file=None,
        application_environment="PROD",
        broker_access_mode="READ_ONLY",
        auth_allowed_origin="https://operator.example.test",
        auth_session_cookie_name="__Host-prod-operator",
        internal_worker_id="worker-prod-1",
        internal_worker_broker_id=str(BROKER_ID),
        internal_worker_token=WORKER_TOKEN,
    )


def authenticator(record: RuntimeRecord | None) -> WorkerAuthenticator:
    return WorkerAuthenticator(settings(), lambda _runtime_id: record)


def test_worker_auth_binds_configured_bearer_instance_secret_and_broker_scope() -> None:
    record = RuntimeRecord(
        str(RUNTIME_ID),
        str(BROKER_ID),
        "worker-prod-1",
        "PROD",
        "READ_ONLY",
        False,
        sha256(RUNTIME_SECRET.encode()).hexdigest(),
        "OWNER",
        2,
        None,
    )
    principal = authenticator(record).authenticate(
        authorization=f"Bearer {WORKER_TOKEN}",
        runtime_instance_header=str(RUNTIME_ID),
        runtime_secret=RUNTIME_SECRET,
    )
    assert principal.runtime_instance_id == RUNTIME_ID
    assert principal.broker_id == BROKER_ID
    assert principal.generation == 2
    assert RUNTIME_SECRET not in repr(principal)


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer wrong", "runtime_instance_header": None, "runtime_secret": None},
        {
            "authorization": f"Bearer {WORKER_TOKEN}",
            "runtime_instance_header": str(RUNTIME_ID),
            "runtime_secret": None,
        },
        {
            "authorization": f"Bearer {WORKER_TOKEN}",
            "runtime_instance_header": str(RUNTIME_ID),
            "runtime_secret": "wrong",
        },
        {
            "authorization": f"Bearer {WORKER_TOKEN}",
            "runtime_instance_header": str(UUID("00000000-0000-4000-8000-000000000099")),
            "runtime_secret": RUNTIME_SECRET,
        },
    ],
)
def test_worker_auth_rejects_bad_or_mismatched_credentials(headers: dict[str, str]) -> None:
    record = RuntimeRecord(
        str(RUNTIME_ID),
        str(BROKER_ID),
        "worker-prod-1",
        "PROD",
        "READ_ONLY",
        False,
        sha256(RUNTIME_SECRET.encode()).hexdigest(),
        "OWNER",
        2,
        None,
    )
    with pytest.raises(WorkerAuthenticationError):
        authenticator(record).authenticate(**headers)


def test_worker_auth_rejects_pending_retired_or_wrongly_scoped_instances() -> None:
    base = RuntimeRecord(
        str(RUNTIME_ID),
        str(BROKER_ID),
        "worker-prod-1",
        "PROD",
        "READ_ONLY",
        False,
        sha256(RUNTIME_SECRET.encode()).hexdigest(),
        "PENDING",
        None,
        None,
    )
    auth = {
        "authorization": f"Bearer {WORKER_TOKEN}",
        "runtime_instance_header": str(RUNTIME_ID),
        "runtime_secret": RUNTIME_SECRET,
    }
    for record in (
        base,
        RuntimeRecord(**{**base.__dict__, "registration_status": "RETIRED", "generation": 1}),
        RuntimeRecord(
            **{
                **base.__dict__,
                "registration_status": "OWNER",
                "generation": 1,
                "user_broker_id": str(UUID(int=9)),
            }
        ),
        RuntimeRecord(
            **{**base.__dict__, "registration_status": "OWNER", "generation": 1, "worker_id": "other-worker"}
        ),
    ):
        with pytest.raises(WorkerAuthenticationError):
            WorkerAuthenticator(settings(), lambda _runtime_id, value=record: value).authenticate(**auth)


def test_runtime_enrollment_accepts_only_exact_configured_bearer() -> None:
    worker_auth = authenticator(None)
    worker_auth.verify_enrollment(f"Bearer {WORKER_TOKEN}")
    with pytest.raises(WorkerAuthenticationError):
        worker_auth.verify_enrollment("Bearer wrong")


def test_prod_internal_worker_routes_require_worker_auth_but_market_api_stays_market_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-prod-operator")
    monkeypatch.setenv("INTERNAL_WORKER_ID", "worker-prod-1")
    monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(BROKER_ID))
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    with TestClient(
        create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'worker-auth.db'}")
    ) as client:
        protected = client.post("/internal/automation-statuses", json={"automation_ids": []})
        market = client.post("/internal/v1/market/snapshots", json={})

    assert protected.status_code == 401
    assert market.status_code == 422


def test_first_prod_runtime_enrollment_uses_headers_and_returns_read_only_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'worker-enrollment.db'}"
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-prod-operator")
    monkeypatch.setenv("INTERNAL_WORKER_ID", "worker-prod-1")
    monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(BROKER_ID))
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO user_brokers "
            "(id, api_slug, display_name, environment, fqdn, settings, external_account_id, state, "
            "created_at, updated_at) "
            "VALUES (?, 't_invest', 'Synthetic', 'PROD', 'invest-public-api.tbank.ru:443', ?, 'account-1', "
            "'ACTIVE', ?, ?)",
            (str(BROKER_ID), '{"token":"synthetic-token"}', "2026-10-09T00:00:00Z", "2026-10-09T00:00:00Z"),
        )
    engine.dispose()
    body = {
        "worker_id": "worker-prod-1",
        "runtime_instance_id": str(RUNTIME_ID),
        "broker_id": str(BROKER_ID),
        "application_environment": "PROD",
        "access_mode": "READ_ONLY",
        "strategy_enabled": False,
    }

    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
        response = client.post(
            "/internal/automaton/runtime-instances",
            json=body,
            headers={
                "Authorization": f"Bearer {WORKER_TOKEN}",
                "X-Runtime-Instance-ID": str(RUNTIME_ID),
                "X-Runtime-Secret": RUNTIME_SECRET,
            },
        )
        admission = client.get(
            f"/internal/automaton/brokers/{BROKER_ID}/admission",
            headers={
                "Authorization": f"Bearer {WORKER_TOKEN}",
                "X-Runtime-Instance-ID": str(RUNTIME_ID),
                "X-Runtime-Secret": RUNTIME_SECRET,
            },
        )
        renewed = client.post(
            f"/internal/automaton/brokers/{BROKER_ID}/admission/renew",
            headers={
                "Authorization": f"Bearer {WORKER_TOKEN}",
                "X-Runtime-Instance-ID": str(RUNTIME_ID),
                "X-Runtime-Secret": RUNTIME_SECRET,
            },
        )
        wrong_scope = client.get(
            f"/internal/automaton/brokers/{UUID(int=9)}/admission",
            headers={
                "Authorization": f"Bearer {WORKER_TOKEN}",
                "X-Runtime-Instance-ID": str(RUNTIME_ID),
                "X-Runtime-Secret": RUNTIME_SECRET,
            },
        )

    assert response.status_code == 200
    assert response.json()["registration_status"] == "OWNER"
    assert response.json()["generation"] == 1
    assert response.json()["admission_status"] == "DISARMED"
    assert RUNTIME_SECRET not in response.text
    assert admission.status_code == 200
    assert admission.json()["owner_runtime_instance_id"] == str(RUNTIME_ID)
    assert renewed.status_code == 200
    assert renewed.json()["generation"] == 1
    assert wrong_scope.status_code == 404
