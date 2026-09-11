"""HTTP contracts for strategy-free trading automation actions."""

from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.automations import TradingAutomationDetails
from moex_sentinel.domain.repository_records import AutomationRecord
from sentinel_contracts.trading import AutomationState
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
        lambda _factory: SimpleNamespace(
            create_trading_automation=create,
            view_trading_automations=listing,
            view_trading_automation=details,
            view_trading_automation_details=enriched,
            hold_automation=hold,
            resume_automation=resume,
            close_automation=close,
        ),
    )

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")) as client:
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
