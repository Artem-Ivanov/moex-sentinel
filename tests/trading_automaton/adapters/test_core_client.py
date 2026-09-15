"""Transport mapping for the baseline Worker-to-Core client."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatus,
    AutomationStatusesResult,
    FactBatchRequest,
    FactBatchResult,
)
from tests.contracts.trading_facts_helpers import all_envelopes
from trading_automaton.adapters.core_client import CoreClient


def command() -> AutomationCommand:
    return AutomationCommand(
        automation_id=UUID("00000000-0000-4000-8000-000000000501"),
        user_broker_id=UUID("00000000-0000-4000-8000-000000000502"),
        broker_id=UUID("00000000-0000-4000-8000-000000000503"),
        account_id="synthetic-account",
        external_instrument_id="synthetic-instrument",
        instrument_id=UUID("00000000-0000-4000-8000-000000000504"),
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        state=AutomationState.IN_WORK,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
    )


def test_calls_only_baseline_typed_routes() -> None:
    requests: list[httpx.Request] = []
    expected_command = command()
    facts = [all_envelopes()[0]]
    expected_result = FactBatchResult(results=())
    statuses = AutomationStatusesResult(
        automations=(
            AutomationStatus(
                automation_id=expected_command.automation_id,
                user_broker_id=expected_command.user_broker_id,
                state=expected_command.state,
                revision=expected_command.revision,
                last_sequence_number=expected_command.last_sequence_number,
                resume_requested=False,
            ),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/internal/automation-commands":
            return httpx.Response(200, json={"commands": [expected_command.model_dump(mode="json")]})
        if request.url.path == "/internal/automation-statuses":
            return httpx.Response(200, json=statuses.model_dump(mode="json"))
        if request.url.path == "/internal/automation-facts":
            return httpx.Response(200, json=expected_result.model_dump(mode="json"))
        raise AssertionError(f"Unexpected route: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        client = CoreClient(http)
        claimed = client.claim_commands("worker-1", 10)
        actual_statuses = client.automation_statuses([expected_command.automation_id])
        result = client.publish_facts(facts)

    assert claimed == [expected_command]
    assert actual_statuses == statuses
    assert result == expected_result
    assert [request.url.path for request in requests] == [
        "/internal/automation-commands",
        "/internal/automation-statuses",
        "/internal/automation-facts",
    ]
    assert all("/v2/" not in request.url.path for request in requests)
    assert all(request.url.path != "/internal/automaton/events" for request in requests)
    assert json.loads(requests[0].content) == {"worker_id": "worker-1", "limit": 10}
    assert json.loads(requests[1].content) == {"automation_ids": [str(expected_command.automation_id)]}
    assert json.loads(requests[2].content) == FactBatchRequest(facts=facts).model_dump(mode="json")


def test_exposes_http_conflicts_without_transport_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(409, json={"detail": {"code": "CONFLICT"}})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http,
        pytest.raises(httpx.HTTPStatusError) as error,
    ):
        CoreClient(http).claim_commands("worker-1", 5)

    assert error.value.response.status_code == 409
    assert attempts == 1


def test_keeps_required_heartbeat_and_broker_connection_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/heartbeats"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "broker_id": "broker-1",
                "adapter_code": "TINVEST_SANDBOX",
                "target": "sandbox-invest-public-api.tbank.ru:443",
                "token": "synthetic-token",
                "is_test": True,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        client = CoreClient(http)
        client.heartbeat("worker-1", datetime(2026, 8, 13, 12, tzinfo=UTC))
        connection = client.broker_connection("broker-1")

    assert connection.adapter_code == "TINVEST_SANDBOX"
    assert [request.url.path for request in requests] == [
        "/internal/automaton/heartbeats",
        "/internal/automaton/brokers/broker-1/connection",
    ]
