from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import hash_password
from moex_sentinel.domain.portfolio import (
    BrokerOperation,
    ExternalOperation,
)
from moex_sentinel.usecases.portfolio import ViewRecentOperationsUsecase


class ScopedOperationsUsecase:
    def __init__(self):
        self.calls = []

    async def view_operations_scoped(self, broker_id, account_id, *, limit, cursor, from_at, to_at):
        self.calls.append((broker_id, account_id, limit, cursor, from_at, to_at))
        operation = ExternalOperation(
            operation_id="op-1",
            account_id=account_id,
            operation_type="BUY",
            state="EXECUTED",
            occurred_at=datetime(2026, 10, 20, 10, tzinfo=UTC),
            payment=None,
            price=None,
            quantity=Decimal("1"),
            commission=None,
            instrument_id="uid-1",
        )
        return SimpleNamespace(
            items=(BrokerOperation(broker_id, "Broker", operation),),
            errors=(),
            input_cursor=cursor,
            next_cursor="next-page",
            source_item_count=500,
            selected_item_count=1,
            source_exhausted=False,
            coverage_complete=False,
        )


def _app(monkeypatch, tmp_path, usecase):
    usecases = SimpleNamespace(
        daily_admission_preparation=SimpleNamespace(),
        view_recent_operations=ViewRecentOperationsUsecase(usecase),
    )
    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)
    return create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'operations.db'}")


def _params(**overrides):
    now = datetime.now(UTC)
    params = {
        "broker_id": "broker-1",
        "account_id": "account-1",
        "from_at": (now - timedelta(days=2)).isoformat(),
        "to_at": (now - timedelta(days=1)).isoformat(),
        "limit": 500,
    }
    return params | overrides


def test_scoped_operations_http_contract_and_cursor_continuation(monkeypatch, tmp_path):
    service = ScopedOperationsUsecase()
    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
        first_params = _params(
            from_at="2026-10-01T03:00:00.001999+03:00",
            to_at="2026-10-20T12:00:00.009999Z",
        )
        first = client.get("/api/operations/scoped", params=first_params)
        second = client.get("/api/operations/scoped", params=first_params | {"cursor": first.json()["next_cursor"]})

    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body == {
        "items": [
            {
                "broker_id": "broker-1",
                "broker_name": "Broker",
                "operation_id": "op-1",
                "account_id": "account-1",
                "operation_type": "BUY",
                "state": "EXECUTED",
                "occurred_at": "2026-10-20T10:00:00Z",
                "payment": None,
                "price": None,
                "quantity": "1",
                "commission": None,
                "instrument_id": "uid-1",
                "ticker": None,
            }
        ],
        "errors": [],
        "input_cursor": None,
        "next_cursor": "next-page",
        "source_item_count": 500,
        "selected_item_count": 1,
        "source_exhausted": False,
        "coverage_complete": False,
    }
    assert service.calls[0][0:4] == ("broker-1", "account-1", 500, None)
    assert service.calls[0][4] == datetime(2026, 10, 1, 0, 0, 0, 1000, tzinfo=UTC)
    assert service.calls[0][5] == datetime(2026, 10, 20, 12, 0, 0, 9000, tzinfo=UTC)
    assert service.calls[1][3] == "next-page"
    assert service.calls[1][4:] == service.calls[0][4:]


@pytest.mark.parametrize(
    "overrides",
    [
        {"from_at": "2026-10-01T00:00:00"},
        {"from_at": "2026-10-21T00:00:00Z"},
        {"from_at": "2026-10-20T00:00:00Z", "to_at": "2026-10-19T00:00:00Z"},
        {"from_at": ""},
        {"to_at": ""},
        {"limit": 0},
        {"limit": 501},
        {"cursor": ""},
        {"cursor": "x" * 1025},
    ],
)
def test_scoped_operations_reject_invalid_query_bounds(monkeypatch, tmp_path, overrides):
    service = ScopedOperationsUsecase()
    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
        response = client.get("/api/operations/scoped", params=_params(**overrides))

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("missing_parameter", ["from_at", "to_at"])
def test_scoped_operations_require_both_date_bounds(monkeypatch, tmp_path, missing_parameter):
    service = ScopedOperationsUsecase()
    params = _params()
    params.pop(missing_parameter)

    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
        response = client.get("/api/operations/scoped", params=params)

    assert response.status_code == 422
    assert service.calls == []


def test_scoped_operations_reject_future_to_with_valid_ordered_bounds(monkeypatch, tmp_path):
    service = ScopedOperationsUsecase()
    now = datetime.now(UTC)
    params = _params(
        from_at=(now - timedelta(hours=1)).isoformat(),
        to_at=(now + timedelta(hours=1)).isoformat(),
    )

    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
        response = client.get("/api/operations/scoped", params=params)

    assert datetime.fromisoformat(params["from_at"]) < datetime.fromisoformat(params["to_at"])
    assert response.status_code == 422
    assert service.calls == []


def test_legacy_operations_endpoint_still_uses_original_limit_contract(monkeypatch, tmp_path):
    class LegacyUsecase:
        async def execute(self, limit):
            assert limit == 20
            return SimpleNamespace(items=(), errors=())

    usecases = SimpleNamespace(daily_admission_preparation=SimpleNamespace(), view_recent_operations=LegacyUsecase())
    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)

    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'legacy.db'}")) as client:
        response = client.get("/api/operations?limit=20")

    assert response.status_code == 200
    assert response.json() == {"items": [], "errors": []}


def test_scoped_operations_remain_behind_operator_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
    service = ScopedOperationsUsecase()
    usecases = SimpleNamespace(
        daily_admission_preparation=SimpleNamespace(),
        view_recent_operations=ViewRecentOperationsUsecase(service),
    )
    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'private-operations.db'}")) as client:
        response = client.get("/api/operations/scoped", params=_params())

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == []
