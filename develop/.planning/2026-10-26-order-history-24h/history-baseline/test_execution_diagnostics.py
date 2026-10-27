from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import hash_password
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
from tests.storage.trading_facts_helpers import NOW, automation_model, instrument_model, user_broker_model


def test_scoped_execution_diagnostics_is_authenticated_bounded_and_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
    broker_id, automation_id, instrument_id = (str(uuid4()) for _ in range(3))
    intent_id = str(uuid4())
    runtime_id = str(uuid4())
    database_url = f"sqlite:///{tmp_path / 'execution-diagnostics.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with sessionmaker(engine).begin() as session:
        session.add(user_broker_model(broker_id, "account-1"))
        session.flush()
        session.add(instrument_model(instrument_id, broker_id))
        session.flush()
        session.add(automation_model(automation_id, user_broker_id=broker_id, instrument_id=instrument_id))
        session.add(
            TradingSubmitReservationModel(
                reservation_id=str(uuid4()),
                user_broker_id=broker_id,
                automation_id=automation_id,
                idempotency_key=intent_id,
                request_fingerprint="a" * 64,
                state="UNKNOWN",
                runtime_instance_id=runtime_id,
                generation=1,
                configuration_fingerprint="b" * 64,
                side="BUY",
                instrument_uid="TEST@TQBR",
                quantity_lots=2,
                limit_price=Decimal("10"),
                requested_amount=Decimal("20"),
                reserved_cash=Decimal("20"),
                reserved_position_units=Decimal("0"),
                payload={"private": "must not be returned"},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    engine.dispose()

    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
        response = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics")
        invalid_limit = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics?limit=21")
        invalid_intent = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics?intent_id=nope")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["automation_id"] == automation_id
    assert payload["broker_id"] == broker_id
    assert payload["worker_observation"]["observation"] == "UNKNOWN"
    assert payload["worker_observation"]["reason"] == "NOT_OBSERVED"
    assert payload["external_instrument_id"] == f"external-{instrument_id}"
    assert payload["currency"] == "RUB"
    assert payload["orders"] == payload["events"] == []
    assert payload["reservations"] == [
        {
            "intent_id": intent_id,
            "state": "UNKNOWN",
            "side": "BUY",
            "origin_runtime_instance_id": runtime_id,
            "origin_generation": 1,
            "quantity_lots": 2,
            "reserved_cash": "20.000000000",
            "reserved_position_units": "0E-9",
            "created_at": payload["reservations"][0]["created_at"],
            "updated_at": payload["reservations"][0]["updated_at"],
        }
    ]
    assert payload["executions"] == payload["accepted_envelopes"] == []
    assert "private" not in response.text
    assert invalid_limit.status_code == invalid_intent.status_code == 422


def test_scoped_execution_diagnostics_requires_operator_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("test password long enough"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'auth.db'}")) as client:
        response = client.get(f"/api/trading-automations/{uuid4()}/execution-diagnostics")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
