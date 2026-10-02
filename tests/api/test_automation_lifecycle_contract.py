"""Lifecycle business conflicts retain the public HTTP response contract."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.environment import PinnedEnvironment
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.usecases.automations import (
    CloseAutomationUsecase,
    HoldAutomationUsecase,
    ResumeAutomationUsecase,
    ViewTradingAutomationUsecase,
)
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model


@pytest.mark.parametrize("command", ["hold", "resume"])
def test_closed_automation_command_returns_http_409(monkeypatch, tmp_path, command: str) -> None:
    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    database_url = f"sqlite:///{tmp_path / 'lifecycle-api.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(user_broker_model("scope-1", "synthetic-account"))
        session.flush()
        session.add(instrument_model("instrument-1", "scope-1"))
        session.flush()
        session.add(automation_model("automation-1", user_broker_id="scope-1", instrument_id="instrument-1"))
    service = AutomationService(AutomationRepository(factory), access_mode="TRADE")
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(
            hold_automation=HoldAutomationUsecase(service),
            resume_automation=ResumeAutomationUsecase(service),
            close_automation=CloseAutomationUsecase(service),
        ),
    )

    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
        closed = client.post("/api/trading-automations/automation-1/close")
        response = client.post(f"/api/trading-automations/automation-1/{command}")
        repeated_close = client.post("/api/trading-automations/automation-1/close")

    assert closed.status_code == 200
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AUTOMATION_STATE_CONFLICT"
    assert repeated_close.json()["revision"] == closed.json()["revision"]
    engine.dispose()


@pytest.mark.parametrize("command", ["view", "hold", "resume", "close"])
def test_foreign_contour_automation_returns_stable_error(monkeypatch, tmp_path, command):
    monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
    service = AutomationService(
        SimpleNamespace(get=lambda key: SimpleNamespace(broker_id="prod")),
        access_mode="TRADE",
        environment=PinnedEnvironment("TEST"),
        brokers=SimpleNamespace(get=lambda key: SimpleNamespace(is_test=False)),
    )
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(
            view_trading_automation=ViewTradingAutomationUsecase(service),
            hold_automation=HoldAutomationUsecase(service),
            resume_automation=ResumeAutomationUsecase(service),
            close_automation=CloseAutomationUsecase(service),
        ),
    )
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'foreign.db'}")) as client:
        response = (
            client.get("/api/trading-automations/foreign")
            if command == "view"
            else client.post(f"/api/trading-automations/foreign/{command}")
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BROKER_ENVIRONMENT_MISMATCH"
