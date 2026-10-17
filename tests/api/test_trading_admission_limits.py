from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import hash_password
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from sentinel_contracts.trading_admission import AdmissionLimits, AdmissionStatus, AdmissionView, SubmitDenialReason


def test_authenticated_limits_put_route_is_registered(tmp_path):
    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'limits.db'}")
    operation = application.openapi()["paths"]["/api/brokers/{broker_id}/trading-admission/limits"]

    assert set(operation) == {"put"}
    assert operation["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/UpdateLimitsRequest"
    )


def test_limits_put_requires_operator_session_csrf_and_maps_revision_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("long synthetic password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
    broker_id = "00000000-0000-4000-8000-000000000001"
    path = f"/api/brokers/{broker_id}/trading-admission/limits"
    payload = {
        "expected_revision": 7,
        "limits": {
            "currency": "RUB",
            "max_order_notional": "1000",
            "exposure_scope": "ACCOUNT_GROSS",
            "max_exposure": "5000",
            "max_outstanding_orders": 3,
            "max_daily_loss": "250",
        },
    }
    calls = []

    with TestClient(
        create_app(database_url=f"sqlite:///{tmp_path / 'limits-auth.db'}"), base_url="http://127.0.0.1:8000"
    ) as client:
        assert client.put(path, json=payload).status_code == 401
        login = client.post("/api/auth/login", json={"username": "operator", "password": "long synthetic password"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert client.put(path, json=payload).status_code == 403

        def stale_update(received_broker_id, request, *, actor):
            calls.append((received_broker_id, request.expected_revision, request.limits.max_exposure, actor))
            raise AdmissionConflictError("stale synthetic revision")

        client.app.state.usecases.trading_admission.update_limits = stale_update
        assert (
            client.put(path, json={"expected_revision": "invalid"}, headers={"X-CSRF-Token": csrf}).status_code == 422
        )
        response = client.put(path, json=payload, headers={"X-CSRF-Token": csrf})

        def successful_update(received_broker_id, request, *, actor):
            assert (received_broker_id, request.expected_revision, actor) == (broker_id, 7, "operator")
            return AdmissionView(
                broker_id=UUID(broker_id),
                account_id="synthetic-account",
                revision=8,
                owner_runtime_instance_id=None,
                owner_generation=None,
                recovery_runtime_instance_id=None,
                admission_status=AdmissionStatus.DISARMED,
                arm_revision=None,
                lease_expires_at=None,
                arm_expires_at=None,
                limits=AdmissionLimits(
                    currency="RUB",
                    max_order_notional=Decimal("1000"),
                    exposure_scope="ACCOUNT_GROSS",
                    max_exposure=Decimal("5000"),
                    max_outstanding_orders=3,
                    max_daily_loss=Decimal("250"),
                ),
                configuration_fingerprint=None,
                baseline=None,
                reconciliation=None,
                effective_can_submit=False,
                effective_denial_reasons=(SubmitDenialReason.NOT_OWNER,),
                unresolved_reservation_count=0,
                outstanding_order_count=0,
                recovery_only=False,
            )

        client.app.state.usecases.trading_admission.update_limits = successful_update
        success = client.put(path, json=payload, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 409
    assert response.json() == {"detail": "Admission revision changed or account is unavailable"}
    assert calls == [(broker_id, 7, Decimal("5000"), "operator")]
    assert success.status_code == 200
    assert success.json()["revision"] == 8
    assert success.json()["limits"]["exposure_scope"] == "ACCOUNT_GROSS"
