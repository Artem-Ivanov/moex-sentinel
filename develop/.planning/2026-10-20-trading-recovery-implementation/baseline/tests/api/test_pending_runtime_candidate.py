from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from moex_sentinel.api import app as app_module
from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import hash_password
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from moex_sentinel.views.trading_admission import router
from sentinel_contracts.trading_admission import PendingRuntimeCandidate

BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
REGISTERED_AT = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def candidate():
    return PendingRuntimeCandidate(
        broker_id=BROKER_ID,
        worker_id="prod-worker",
        runtime_instance_id=RUNTIME_ID,
        environment="PROD",
        access_mode="TRADE",
        strategy_enabled=True,
        registration_status="PENDING",
        registered_at=REGISTERED_AT,
    )


class AdmissionStub:
    def __init__(self):
        self.calls = []
        self.result = candidate()

    def pending_runtime_candidate(self, broker_id, worker_id, registered_after):
        self.calls.append((broker_id, worker_id, registered_after))
        if broker_id == "none":
            return None
        if broker_id == "ambiguous":
            raise AdmissionConflictError("ambiguous pending runtime")
        return self.result


def test_pending_runtime_endpoint_returns_only_scoped_public_candidate():
    admission = AdmissionStub()
    app = FastAPI()
    app.state.usecases = SimpleNamespace(trading_admission=admission)
    app.include_router(router, prefix="/api")
    path = f"/api/brokers/{BROKER_ID}/trading-admission/pending-runtime"
    with TestClient(app) as client:
        response = client.get(
            path,
            params={"worker_id": "prod-worker", "registered_after": "2026-10-17T09:59:59.000Z"},
        )
        missing = client.get(
            path.replace(str(BROKER_ID), "none"),
            params={"worker_id": "prod-worker", "registered_after": "2026-10-17T09:59:59.000Z"},
        )
        ambiguous = client.get(
            path.replace(str(BROKER_ID), "ambiguous"),
            params={"worker_id": "prod-worker", "registered_after": "2026-10-17T09:59:59.000Z"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "broker_id": str(BROKER_ID),
        "worker_id": "prod-worker",
        "runtime_instance_id": str(RUNTIME_ID),
        "environment": "PROD",
        "access_mode": "TRADE",
        "strategy_enabled": True,
        "registration_status": "PENDING",
        "registered_at": "2026-10-17T10:00:00.000Z",
    }
    assert "secret" not in response.text.lower()
    assert "token" not in response.text.lower()
    assert missing.status_code == 404
    assert ambiguous.status_code == 409
    assert len(admission.calls) == 3
    assert admission.calls[0] == (str(BROKER_ID), "prod-worker", datetime(2026, 10, 17, 9, 59, 59, tzinfo=UTC))


def test_pending_runtime_endpoint_rejects_non_utc_or_millisecond_cutoff():
    admission = AdmissionStub()
    app = FastAPI()
    app.state.usecases = SimpleNamespace(trading_admission=admission)
    app.include_router(router, prefix="/api")
    path = f"/api/brokers/{BROKER_ID}/trading-admission/pending-runtime"
    with TestClient(app) as client:
        no_timezone = client.get(path, params={"worker_id": "prod-worker", "registered_after": "2026-10-17T10:00:00"})
        excessive_precision = client.get(
            path, params={"worker_id": "prod-worker", "registered_after": "2026-10-17T10:00:00.000001Z"}
        )

    assert no_timezone.status_code == 422
    assert excessive_precision.status_code == 422
    assert admission.calls == []


def test_pending_runtime_endpoint_uses_operator_auth_middleware(tmp_path, monkeypatch):
    class Gateway:
        async def close(self):
            return None

    admission = AdmissionStub()
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("pending runtime test password"))
    monkeypatch.setattr(
        app_module,
        "build_application_usecases",
        lambda *_args, **_kwargs: SimpleNamespace(
            trading_admission=admission,
            daily_admission_preparation=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: Gateway())

    async def observe():
        return None

    monkeypatch.setattr(app_module, "_build_readiness_observer", lambda *_args, **_kwargs: observe)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'operator-auth.db'}")
    path = f"/api/brokers/{BROKER_ID}/trading-admission/pending-runtime"
    with TestClient(app) as client:
        response = client.get(
            path,
            params={"worker_id": "prod-worker", "registered_after": "2026-10-17T09:59:59.000Z"},
        )

    assert response.status_code == 401
    assert admission.calls == []
