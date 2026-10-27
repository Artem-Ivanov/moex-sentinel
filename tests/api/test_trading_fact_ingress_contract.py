"""HTTP contract for baseline command, status and fact routes."""

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from moex_sentinel.api.app import create_app
from moex_sentinel.api.worker_auth import WorkerPrincipal
from moex_sentinel.views.internal_trading_facts import publish_automation_facts
from moex_sentinel.views.schemas.trading_facts import PublishFactsRequestSchema
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatus,
    AutomationStatusesResult,
    FactBatchResult,
    FactGroupAcknowledgement,
)
from tests.contracts.trading_facts_helpers import all_envelopes

SCOPE_ID = UUID("00000000-0000-4000-8000-000000000002")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000004")


class Execute:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.keyword_calls: list[dict[str, object]] = []

    def execute(self, *args: object, **kwargs: object) -> object:
        self.calls.append(args)
        self.keyword_calls.append(kwargs)
        return self.result


def test_baseline_routes_delegate_strict_typed_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = AutomationCommand(
        automation_id=AUTOMATION_ID,
        user_broker_id=SCOPE_ID,
        broker_id=SCOPE_ID,
        account_id="synthetic-account",
        external_instrument_id="synthetic-instrument",
        instrument_id=INSTRUMENT_ID,
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        state=AutomationState.IN_QUEUE,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        bootstrap=None,
    )
    statuses_result = AutomationStatusesResult(
        automations=(
            AutomationStatus(
                automation_id=AUTOMATION_ID,
                user_broker_id=SCOPE_ID,
                state=AutomationState.IN_QUEUE,
                revision=1,
                last_sequence_number=0,
                resume_requested=False,
            ),
        )
    )
    facts = list(all_envelopes())
    batch_result = FactBatchResult(
        results=(
            FactGroupAcknowledgement(
                automation_id=AUTOMATION_ID,
                accepted_through_sequence=9,
                current_revision=2,
                accepted_event_ids=tuple(item.event_id for item in facts),
            ),
        )
    )
    claims = Execute([command])
    statuses = Execute(statuses_result)
    publish = Execute(batch_result)
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(
            daily_admission_preparation=SimpleNamespace(),
            claim_automation_commands=claims,
            view_automation_statuses=statuses,
            publish_trading_facts=publish,
        ),
    )

    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'typed-api.db'}")) as client:
        commands_response = client.post(
            "/internal/automation-commands",
            json={"worker_id": "synthetic-worker", "limit": 10},
        )
        statuses_response = client.post(
            "/internal/automation-statuses",
            json={"automation_ids": [str(AUTOMATION_ID)]},
        )
        facts_response = client.post(
            "/internal/automation-facts",
            json={"facts": [item.model_dump(mode="json") for item in facts]},
        )

    assert commands_response.status_code == 200
    assert commands_response.json()["commands"][0]["instrument_id"] == str(INSTRUMENT_ID)
    assert commands_response.json()["commands"][0]["currency"] == "RUB"
    assert statuses_response.status_code == 200
    assert statuses_response.json()["automations"][0]["revision"] == 1
    assert "commission_statuses" not in statuses_response.json()
    assert facts_response.status_code == 200
    assert facts_response.json()["results"][0]["accepted_through_sequence"] == 9
    assert claims.calls == [(10,)]
    assert statuses.calls == [([AUTOMATION_ID],)]
    assert len(cast(list[object], publish.calls[0][0])) == 9


def test_openapi_exposes_only_baseline_worker_routes(tmp_path: Path) -> None:
    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'openapi.db'}")
    paths = set(application.openapi()["paths"])

    assert {
        "/internal/automation-commands",
        "/internal/automation-statuses",
        "/internal/automation-facts",
    } <= paths
    assert not any("/internal/v2" in path for path in paths)
    assert "/internal/automaton/commands" not in paths
    assert "/internal/automaton/events" not in paths
    assert "/internal/automaton/automations/statuses" not in paths


def test_malformed_fact_batch_returns_422_without_calling_usecase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    publish = Execute(FactBatchResult(results=()))
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory, *, settings: SimpleNamespace(
            daily_admission_preparation=SimpleNamespace(), publish_trading_facts=publish
        ),
    )
    malformed = all_envelopes()[0].model_dump(mode="json")
    malformed["payload"]["unexpected"] = True

    with TestClient(
        create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'invalid-typed-api.db'}")
    ) as client:
        response = client.post("/internal/automation-facts", json={"facts": [malformed]})

    assert response.status_code == 422
    assert publish.calls == []
    assert response.json()["detail"]["fields"][0]["path"].endswith("payload.unexpected")
    assert "Synthetic fact" not in response.text


def test_fact_route_passes_only_runtime_identity_scalars_to_usecase(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_id = UUID("00000000-0000-4000-8000-000000000071")
    publish = Execute(FactBatchResult(results=()))
    monkeypatch.setattr(
        "moex_sentinel.views.internal_trading_facts._usecases",
        lambda _request: SimpleNamespace(publish_trading_facts=publish),
    )

    async def direct_run_sync(function):
        return function()

    monkeypatch.setattr("moex_sentinel.views.internal_trading_facts.run_sync", direct_run_sync)
    request = Request({"type": "http", "method": "POST", "path": "/internal/automation-facts", "headers": []})
    request.state.worker_principal = WorkerPrincipal(
        worker_id="worker-prod",
        runtime_instance_id=runtime_id,
        broker_id=SCOPE_ID,
        environment="PROD",
        access_mode="TRADE",
        strategy_enabled=True,
        generation=7,
        registration_status="OWNER",
        recovery_only=False,
    )
    facts = [all_envelopes()[0]]

    result = asyncio.run(publish_automation_facts(PublishFactsRequestSchema(facts=facts), request))

    assert result == FactBatchResult(results=())
    assert publish.calls == [(facts,)]
    assert publish.keyword_calls == [{"runtime_instance_id": runtime_id, "generation": 7}]


def test_fact_route_rejects_scope_mismatch_before_usecase(monkeypatch: pytest.MonkeyPatch) -> None:
    publish = Execute(FactBatchResult(results=()))
    monkeypatch.setattr(
        "moex_sentinel.views.internal_trading_facts._usecases",
        lambda _request: SimpleNamespace(publish_trading_facts=publish),
    )
    request = Request({"type": "http", "method": "POST", "path": "/internal/automation-facts", "headers": []})
    request.state.worker_principal = WorkerPrincipal(
        worker_id="worker-prod",
        runtime_instance_id=UUID("00000000-0000-4000-8000-000000000071"),
        broker_id=SCOPE_ID,
        environment="PROD",
        access_mode="TRADE",
        strategy_enabled=True,
        generation=7,
        registration_status="OWNER",
        recovery_only=False,
    )
    foreign_fact = all_envelopes()[0].model_copy(
        update={"user_broker_id": UUID("00000000-0000-4000-8000-000000000099")}
    )

    response = asyncio.run(publish_automation_facts(PublishFactsRequestSchema(facts=[foreign_fact]), request))

    assert response.status_code == 403
    assert publish.calls == []
