"""HTTP contracts for strategy-free trading automation actions."""

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.automations import TradingAutomationDetails
from moex_sentinel.domain.portfolio import BrokerOperationsView
from moex_sentinel.domain.repository_records import AutomationRecord
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.usecases.automations import ViewTradingAutomationDetailsUsecase, ViewTradingAutomationsUsecase
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading import AutomationState
from tests.storage.trading_facts_helpers import NOW, automation_model, instrument_model, user_broker_model
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.services.strategies import AdaptiveScalpingStrategy


def automation(**changes: object) -> AutomationRecord:
    return AutomationRecord(
        id="automation-1",
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        state=AutomationState.IN_QUEUE,
        suspended_from_state=None,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        currency="RUB",
        quantity_lots=0,
        average_price=Decimal(),
        invested_amount=Decimal(),
        realized_pnl=Decimal(),
        unrealized_pnl=Decimal(),
        net_pnl=Decimal(),
        actual_commissions=Decimal(),
        broker_name="Synthetic broker",
        ticker="TEST",
        instrument_name="Synthetic instrument",
    ).model_copy(update=changes)


class Execute:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def execute(self, *args: object) -> object:
        self.calls.append(args)
        return self.result


class AsyncExecute(Execute):
    async def execute(self, *args: object) -> object:
        return super().execute(*args)


@pytest.mark.parametrize(
    ("environment", "access_mode"), [("PROD", "READ_ONLY"), ("TEST", "READ_ONLY"), ("TEST", "TRADE")]
)
def test_internal_connection_http_preserves_worker_broker_scope(monkeypatch, tmp_path, environment, access_mode):
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", environment)
    monkeypatch.setenv("BROKER_ACCESS_MODE", access_mode)
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-test-prod-session")
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "false")
    expected = BrokerConnection(
        broker_id="broker-1",
        adapter_code="TINVEST_PROD" if environment == "PROD" else "TINVEST_SANDBOX",
        target="invest-public-api.tbank.ru:443" if environment == "PROD" else "sandbox-invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - synthetic fixture
        is_test=environment == "TEST",
        environment=environment,
        access_mode=access_mode,
        account_id="selected-account",
    )
    connection = Execute(expected)
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(view_automaton_broker_connection=connection),
    )
    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'connection.db'}")) as api:

        def forward(request):
            response = api.request(request.method, request.url.path, content=request.content)
            return httpx.Response(response.status_code, content=response.content)

        with httpx.Client(base_url="http://core", transport=httpx.MockTransport(forward)) as http:
            actual = CoreClient(http, application_environment=environment, access_mode=access_mode).broker_connection(
                "broker-1"
            )

    assert connection.calls == [("broker-1",)]
    assert actual.account_id == expected.account_id
    assert actual.environment == expected.environment
    assert actual.access_mode == expected.access_mode
    assert actual.token == expected.token
    assert actual == expected


def test_public_automation_contract_is_strategy_free(monkeypatch, tmp_path) -> None:
    create = AsyncExecute(automation())
    listing = Execute([automation()])
    details = Execute(automation())
    enriched = AsyncExecute(TradingAutomationDetails(automation(), (), (), ()))
    hold = Execute(automation(state=AutomationState.HOLD, revision=2))
    resume = Execute(automation(revision=3))
    close = Execute(automation(state=AutomationState.CLOSED, revision=4))
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(
            create_trading_automation=create,
            view_trading_automations=listing,
            view_trading_automation=details,
            view_trading_automation_details=enriched,
            hold_automation=hold,
            resume_automation=resume,
            close_automation=close,
        ),
    )

    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'api.db'}")) as client:
        created = client.post(
            "/api/instruments/broker-1/instrument-1/trade",
            json={"account_id": "account-1"},
        )
        listed = client.get("/api/trading-automations")
        viewed = client.get("/api/trading-automations/automation-1")
        enriched_response = client.get("/api/trading-automations/automation-1/details")
        held = client.post("/api/trading-automations/automation-1/hold")
        resumed = client.post("/api/trading-automations/automation-1/resume")
        closed = client.post("/api/trading-automations/automation-1/close")

    assert created.status_code == 201
    assert create.calls == [("broker-1", "account-1", "instrument-1")]
    assert listed.json()["items"][0]["currency"] == "RUB"
    assert "strategy" not in viewed.json()
    strategy = AdaptiveScalpingStrategy()
    for payload in (
        created.json(),
        listed.json()["items"][0],
        viewed.json(),
        enriched_response.json()["automation"],
        held.json(),
        resumed.json(),
        closed.json(),
    ):
        assert (payload["strategy_code"], payload["strategy_version"]) == (strategy.code, strategy.version)
    assert enriched_response.json()["automation"]["id"] == "automation-1"
    assert held.json()["state"] == "HOLD"
    assert resumed.json()["state"] == "IN_QUEUE"
    assert closed.json()["state"] == "CLOSED"


@pytest.mark.parametrize(
    ("state", "snapshot", "sequence", "expected"),
    [
        ("HOLD", "complete", 0, True),
        ("IN_QUEUE", "complete", 0, True),
        ("HOLD", "none", 0, False),
        ("HOLD", "partial", 0, False),
        ("IN_WORK", "complete", 4, False),
        ("HOLD", "complete", 4, False),
    ],
)
def test_api_distinguishes_pending_bootstrap_from_confirmed_position(
    monkeypatch, tmp_path, state, snapshot, sequence, expected
) -> None:
    database_url = f"sqlite:///{tmp_path / 'bootstrap-api.db'}"
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        with factory.begin() as session:
            if snapshot == "partial":
                # Corrupt baseline defense is tested only on this isolated SQLite fixture.
                session.execute(text("PRAGMA ignore_check_constraints = ON"))
            session.add(user_broker_model("scope-1", "account-1"))
            session.flush()
            session.add(instrument_model("instrument-1", "scope-1"))
            session.flush()
            record = automation_model("automation-1", state=state)
            record.last_sequence_number = sequence
            if snapshot != "none":
                record.hold_reason = "BOOTSTRAPPING" if state == "HOLD" else None
                record.bootstrap_position_cycle_id = "bootstrap-cycle"
                record.bootstrap_position_lot_id = "bootstrap-lot"
                record.bootstrap_quantity_lots = 2
                record.bootstrap_average_price = Decimal("100")
                record.bootstrap_invested_amount = Decimal("2000")
                record.bootstrap_currency = None if snapshot == "partial" else "RUB"
                record.bootstrap_observed_at = NOW
            session.add(record)
    finally:
        engine.dispose()

    class ExternalReads:
        async def view_position_operations(self, *args):
            return BrokerOperationsView((), ())

        async def candles(self, *args):
            return ()

    def usecases(factory, *, settings):
        service = AutomationService(AutomationRepository(factory), access_mode=settings.broker_access_mode)
        external = ExternalReads()
        return SimpleNamespace(
            view_trading_automations=ViewTradingAutomationsUsecase(service),
            view_trading_automation_details=ViewTradingAutomationDetailsUsecase(service, external, external),
        )

    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", usecases)
    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
        listed = client.get("/api/trading-automations")
        details = client.get("/api/trading-automations/automation-1/details")

    assert listed.status_code == details.status_code == 200
    for payload in (listed.json()["items"][0], details.json()["automation"]):
        assert payload["bootstrap_pending"] is expected
        assert payload["quantity_lots"] == 0
        assert Decimal(payload["average_price"]) == 0
