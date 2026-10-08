"""Actual Core composition renders missing broker scopes as HTTP 404."""

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.models import Base

MISSING = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", f"/api/brokers/{MISSING}/check", {}),
        ("GET", f"/api/brokers/{MISSING}/accounts", {}),
        ("GET", f"/api/brokers/{MISSING}/market/instruments", {"params": {"query": "SBER"}}),
        ("GET", f"/api/brokers/{MISSING}/market/instruments/instrument", {}),
        (
            "GET",
            f"/api/brokers/{MISSING}/market/instruments/instrument/candles",
            {"params": {"from": "2026-10-06T10:00:00Z", "to": "2026-10-06T11:00:00Z", "interval": "1_MIN"}},
        ),
        ("POST", f"/api/brokers/{MISSING}/instruments/synchronize", {}),
        ("GET", f"/api/brokers/{MISSING}/instruments", {}),
        ("GET", f"/api/brokers/{MISSING}/instruments/instrument", {}),
        ("PUT", f"/api/brokers/{MISSING}/instruments/instrument/selection", {"json": {"selected": True}}),
        ("GET", f"/internal/automaton/brokers/{MISSING}/connection", {}),
        ("GET", f"/internal/automaton/brokers/{MISSING}/scope", {}),
        ("POST", f"/api/instruments/{MISSING}/instrument/trade", {"json": {"account_id": "account"}}),
    ],
)
def test_missing_actual_broker_scope_keeps_http_404(method, path, kwargs, monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")

    def forbidden_adapter(*_args, **_kwargs):
        raise AssertionError("Missing broker scope must not create a broker adapter")

    for name in ("TInvestPortfolioAdapter", "TInvestMarketDataAdapter", "TInvestOrderExecutionAdapter"):
        monkeypatch.setattr(f"moex_sentinel.composition.{name}", forbidden_adapter)
    app = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'core.db'}")
    with TestClient(app) as client:
        Base.metadata.create_all(app.state.database_engine)
        response = client.request(method, path, **kwargs)
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "BROKER_NOT_FOUND", "message": "Подключение брокера не найдено.", "fields": []}
    }
