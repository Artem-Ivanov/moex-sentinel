"""Transport mapping for the baseline Worker-to-Core client."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_admission import (
    AdmissionStatus,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    RuntimeRegistrationStatus,
    SubmitAttemptRequest,
)
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatus,
    AutomationStatusesResult,
    FactBatchRequest,
    FactBatchResult,
)
from sentinel_contracts.version import SERVICE_VERSION
from sentinel_contracts.worker_diagnostics import WorkerDiagnostics, WorkerExecutionDiagnostics
from tests.contracts.trading_facts_helpers import all_envelopes
from tests.services.test_runtime_versions import NOW, progress
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.domain.errors import CoreOperationError


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
        pytest.raises(CoreOperationError) as error,
    ):
        CoreClient(http).claim_commands("worker-1", 5)

    assert error.value.status_code == 409
    assert attempts == 1


@pytest.mark.parametrize("marker", [None, "v2", ""])
def test_heartbeat_omits_execution_diagnostics_without_v1_capability(marker) -> None:
    heartbeat_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/runtime":
            config = {"environment": "TEST", "access_mode": "READ_ONLY"}
            if marker is not None:
                config["execution_diagnostics"] = marker
            return httpx.Response(200, json=config)
        if request.url.path.endswith("/heartbeats"):
            heartbeat_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected route: {request.url.path}")

    diagnostics = WorkerDiagnostics(
        completed_iterations=0,
        last_completed_at=None,
        last_finished_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_result="OUTBOX_BLOCKED",
        outbox={"reason": "READ_FAILED"},
        execution_diagnostics=WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER"),
    )
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        CoreClient(
            http,
            application_environment="TEST",
            access_mode="READ_ONLY",
        ).heartbeat("worker", datetime(2026, 10, 20, tzinfo=UTC), diagnostics)

    assert len(heartbeat_bodies) == 1
    assert "execution_diagnostics" not in heartbeat_bodies[0]["worker_diagnostics"]


def test_heartbeat_sends_execution_diagnostics_when_core_advertises_v1() -> None:
    heartbeat_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/runtime":
            return httpx.Response(
                200,
                json={"environment": "TEST", "access_mode": "READ_ONLY", "execution_diagnostics": "v1"},
            )
        if request.url.path.endswith("/heartbeats"):
            heartbeat_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected route: {request.url.path}")

    diagnostics = WorkerDiagnostics(
        completed_iterations=0,
        last_completed_at=None,
        last_finished_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_result="OUTBOX_BLOCKED",
        outbox={"reason": "READ_FAILED"},
        execution_diagnostics=WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER"),
    )
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        CoreClient(
            http,
            application_environment="TEST",
            access_mode="READ_ONLY",
        ).heartbeat("worker", datetime(2026, 10, 20, tzinfo=UTC), diagnostics)

    assert heartbeat_bodies[0]["worker_diagnostics"]["execution_diagnostics"]["reason"] == "UNCONFIGURED_BROKER"


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


def test_heartbeat_uses_stable_instance_and_actual_shared_version():
    bodies = []

    def handle(request):
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "TEST", "access_mode": "READ_ONLY"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    with httpx.Client(base_url="http://core", transport=httpx.MockTransport(handle)) as http:
        first = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
        first.heartbeat("worker", datetime.now(UTC))
        first.heartbeat("worker", datetime.now(UTC))
        second = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
        second.heartbeat("worker", datetime.now(UTC))
    one, two, three = [body["runtime_version"] for body in bodies]
    assert one == two
    assert one["version"] == SERVICE_VERSION
    assert one["environment"] == "TEST"
    assert one["access_mode"] == "READ_ONLY"
    assert UUID(one["instance_id"]) != UUID(three["instance_id"])


def test_worker_diagnostics_is_additive_to_existing_identity_and_http_heartbeat():
    bodies = []

    def handle(request):
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "TEST", "access_mode": "READ_ONLY"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    with httpx.Client(base_url="http://core", transport=httpx.MockTransport(handle)) as http:
        worker = CoreClient(http, application_environment="TEST", access_mode="READ_ONLY")
        worker.heartbeat("worker", NOW, progress())
    assert bodies[0]["worker_diagnostics"]["completed_iterations"] == 1
    assert bodies[0]["worker_diagnostics"]["outbox"]["pending_count"] == 0
    assert bodies[0]["runtime_version"]["environment"] == "TEST"


@pytest.mark.parametrize("status", [302, 409, 503])
def test_fact_failure_is_safe_and_neutral_with_explicit_retry_policy(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"secret": "NEVER_PUBLIC"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://core.test") as http,
        pytest.raises(CoreOperationError) as caught,
    ):
        CoreClient(http).publish_facts([all_envelopes()[0]])
    assert caught.value.status_code == status
    assert caught.value.retryable is (status >= 500)
    assert str(caught.value) == "Core operation failed"
    assert len(calls) == 1


def test_fact_transport_failure_has_no_protocol_status():
    def handler(request):
        raise httpx.ConnectError("SECRET", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://core.test") as http,
        pytest.raises(CoreOperationError) as caught,
    ):
        CoreClient(http).publish_facts([all_envelopes()[0]])
    assert caught.value.retryable is True
    assert caught.value.status_code is None
    assert "SECRET" not in str(caught.value)


def test_prod_runtime_registration_sends_process_secret_only_in_headers() -> None:
    requests: list[httpx.Request] = []
    broker_id = UUID("00000000-0000-4000-8000-000000000503")
    runtime_id: UUID | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert runtime_id is not None
        result = RuntimeRegistrationResult(
            runtime_instance_id=runtime_id,
            generation=1,
            registration_status=RuntimeRegistrationStatus.OWNER,
            admission_status=AdmissionStatus.DISARMED,
            lease_expires_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="TRADE",
            worker_id="worker-prod",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        runtime_id = client.runtime_instance_id
        registration = RuntimeRegistrationRequest(
            worker_id="worker-prod",
            runtime_instance_id=runtime_id,
            broker_id=broker_id,
            application_environment="PROD",
            access_mode="TRADE",
            strategy_enabled=True,
        )
        actual = client.register_runtime(registration)

    request = requests[0]
    assert request.url.path == "/internal/automaton/runtime-instances"
    assert request.headers["Authorization"] == f"Bearer {'a' * 64}"
    assert request.headers["X-Runtime-Instance-ID"] == str(runtime_id)
    assert len(request.headers["X-Runtime-Secret"]) == 64
    assert "X-Runtime-Secret" not in request.content.decode()
    assert actual.runtime_instance_id == runtime_id


def test_prod_owner_renews_admission_before_heartbeat():
    requests = []
    runtime_instance_id = None

    def handler(request):
        nonlocal runtime_instance_id
        requests.append(request)
        if request.url.path == "/internal/automaton/runtime-instances":
            body = json.loads(request.content)
            runtime_instance_id = body["runtime_instance_id"]
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": body["runtime_instance_id"],
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:00.000Z",
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        if request.url.path.endswith("/admission/renew"):
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:10.000Z",
                },
            )
        return httpx.Response(200, json={})

    broker_id = UUID("00000000-0000-4000-8000-000000000503")
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-prod",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        client.heartbeat("worker-prod", datetime(2026, 10, 9, 12, tzinfo=UTC))

    paths = [request.url.path for request in requests]
    assert paths == [
        "/internal/automaton/runtime-instances",
        "/internal/runtime",
        f"/internal/automaton/brokers/{broker_id}/admission/renew",
        "/internal/automaton/heartbeats",
    ]
    assert all(request.headers["X-Runtime-Instance-ID"] for request in requests)
    assert all(request.headers["Authorization"] == f"Bearer {'a' * 64}" for request in requests)


def test_prod_pending_registration_retries_same_identity_until_owner() -> None:
    requests: list[httpx.Request] = []
    registrations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal registrations
        requests.append(request)
        if request.url.path.endswith("/runtime-instances"):
            registrations += 1
            body = json.loads(request.content)
            status = "PENDING" if registrations == 1 else "OWNER"
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": body["runtime_instance_id"],
                    "generation": None if status == "PENDING" else 2,
                    "registration_status": status,
                    "admission_status": "DISARMED",
                    "lease_expires_at": None if status == "PENDING" else "2099-01-01T00:00:00.000Z",
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        return httpx.Response(
            200,
            json={
                "broker_id": str(broker_id),
                "adapter_code": "TINVEST",
                "target": "invest-public-api.tbank.ru:443",
                "token": "synthetic-token",
                "is_test": False,
                "environment": "PROD",
                "access_mode": "READ_ONLY",
                "account_id": "account",
                "configuration_fingerprint": "a" * 64,
            },
        )

    broker_id = UUID("00000000-0000-4000-8000-000000000504")
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-pending",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        first_instance = client.runtime_instance_id
        assert client.runtime_generation is None
        with pytest.raises(CoreOperationError) as pending:
            client.heartbeat("worker-pending", NOW)
        assert client.runtime_generation is None
        client.broker_connection(str(broker_id))

    registrations_seen = [request for request in requests if request.url.path.endswith("/runtime-instances")]
    assert len(registrations_seen) == 2
    assert client.runtime_generation == 2
    assert pending.value.retryable is True
    assert [json.loads(request.content)["runtime_instance_id"] for request in registrations_seen] == [
        str(first_instance),
        str(first_instance),
    ]
    assert registrations_seen[0].headers["X-Runtime-Secret"] == registrations_seen[1].headers["X-Runtime-Secret"]


def test_prod_pending_runtime_cannot_claim_commands_or_fetch_connection() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/runtime-instances"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": body["runtime_instance_id"],
                    "generation": None,
                    "registration_status": "PENDING",
                    "admission_status": "DISARMED",
                    "lease_expires_at": None,
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        pytest.fail(f"Pending runtime made protected Core request: {request.url.path}")

    broker_id = UUID("00000000-0000-4000-8000-000000000505")
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-pending",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        with pytest.raises(CoreOperationError) as error:
            client.broker_connection(str(broker_id))

    assert error.value.retryable is True
    assert f"/internal/automaton/brokers/{broker_id}/connection" not in calls


def test_prod_owner_generation_change_during_renewal_is_not_reenrolled():
    requests: list[str] = []
    runtime_instance_id = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal runtime_instance_id
        requests.append(request.url.path)
        if request.url.path.endswith("/runtime-instances"):
            body = json.loads(request.content)
            runtime_instance_id = body["runtime_instance_id"]
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:00.000Z",
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        if request.url.path.endswith("/admission/renew"):
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 2,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:10.000Z",
                },
            )
        pytest.fail(f"Expired owner made protected Core request: {request.url.path}")

    broker_id = UUID("00000000-0000-4000-8000-000000000506")
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-owner",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        with pytest.raises(CoreOperationError):
            client.heartbeat("worker-owner", NOW)
        with pytest.raises(CoreOperationError):
            client.broker_connection(str(broker_id))

    assert requests.count("/internal/automaton/runtime-instances") == 1
    assert not any(path.endswith("/heartbeats") for path in requests)


def test_prod_malformed_renewal_identity_closes_runtime_without_reenrollment():
    requests: list[str] = []
    runtime_instance_id = None
    broker_id = UUID("00000000-0000-4000-8000-000000000509")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal runtime_instance_id
        requests.append(request.url.path)
        if request.url.path.endswith("/runtime-instances"):
            body = json.loads(request.content)
            runtime_instance_id = body["runtime_instance_id"]
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:00.000Z",
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        if request.url.path.endswith("/admission/renew"):
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": "malformed",
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:10.000Z",
                },
            )
        pytest.fail(f"Malformed renewal identity made a protected Core request: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-owner",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        with pytest.raises(CoreOperationError):
            client.heartbeat("worker-owner", NOW)
        with pytest.raises(CoreOperationError):
            client.submit_attempt(str(broker_id), "request-id", SubmitAttemptRequest())

    assert requests.count("/internal/automaton/runtime-instances") == 1


@pytest.mark.parametrize("failure_status", [500, 409, "timeout"])
def test_prod_owner_renew_failure_does_not_locally_revoke_read_or_attempt_access(failure_status):
    requests: list[str] = []
    runtime_instance_id = None
    renewals = 0
    broker_id = UUID("00000000-0000-4000-8000-000000000507")
    intent_id = UUID("00000000-0000-4000-8000-000000000508")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal runtime_instance_id, renewals
        requests.append(request.url.path)
        if request.url.path.endswith("/runtime-instances"):
            body = json.loads(request.content)
            runtime_instance_id = body["runtime_instance_id"]
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:00.000Z",
                },
            )
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        if request.url.path.endswith("/admission/renew"):
            renewals += 1
            if renewals == 1 or failure_status == 409:
                if failure_status == "timeout":
                    raise httpx.ReadTimeout("synthetic renewal timeout", request=request)
                return httpx.Response(failure_status)
            return httpx.Response(
                200,
                json={
                    "runtime_instance_id": runtime_instance_id,
                    "generation": 1,
                    "registration_status": "OWNER",
                    "admission_status": "DISARMED",
                    "lease_expires_at": "2099-01-01T00:00:10.000Z",
                },
            )
        if request.url.path.endswith(f"/submit-attempts/{intent_id}"):
            return httpx.Response(
                200,
                json={
                    "broker_id": str(broker_id),
                    "account_id": "account",
                    "idempotency_key": str(intent_id),
                    "consumed_state": None,
                    "proven_not_granted": False,
                    "original_runtime_instance_id": None,
                    "original_generation": None,
                    "original_generation_retired": False,
                    "absence_basis": None,
                },
            )
        if request.url.path.endswith("/heartbeats"):
            return httpx.Response(200, json={})
        pytest.fail(f"Unexpected Core request: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(
            http,
            application_environment="PROD",
            access_mode="READ_ONLY",
            worker_id="worker-owner",
            broker_id=broker_id,
            worker_token="a" * 64,
        )
        with pytest.raises(CoreOperationError):
            client.heartbeat("worker-owner", NOW)
        attempt = client.submit_attempt(str(broker_id), str(intent_id), SubmitAttemptRequest())
        if failure_status == 409:
            with pytest.raises(CoreOperationError):
                client.heartbeat("worker-owner", NOW)
        else:
            client.heartbeat("worker-owner", NOW)

    assert attempt.idempotency_key == intent_id
    assert renewals == 2
    assert requests.count("/internal/automaton/runtime-instances") == 1
    assert requests.count("/internal/automaton/heartbeats") == (0 if failure_status == 409 else 1)
