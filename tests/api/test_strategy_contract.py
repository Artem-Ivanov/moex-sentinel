"""Persisted strategy writes are absent from the baseline HTTP API."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app


def test_strategy_routes_are_absent_and_creation_rejects_strategy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory: SimpleNamespace(),
    )
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'strategy-free.db'}")) as client:
        template = client.get("/api/strategy-templates")
        patch = client.patch(
            "/api/trading-automations/automation-1/strategy",
            json={"expected_revision": 1, "strategy": {}},
        )
        create = client.post(
            "/api/instruments/broker-1/instrument-1/trade",
            json={"account_id": "account-1", "strategy": {}},
        )

    assert template.status_code == 404
    assert patch.status_code == 404
    assert create.status_code == 422
