"""HTTP adapter for the Core internal automaton contract."""

import secrets
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import TypeAdapter

from sentinel_contracts.audit import current_process_id
from sentinel_contracts.broker_execution import BrokerConnection, BrokerScope
from sentinel_contracts.trading_admission import (
    AdmissionView,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    SubmitAttemptRequest,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
)
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatusesResult,
    FactBatchRequest,
    FactBatchResult,
    FactEnvelope,
)
from sentinel_contracts.version import SERVICE_VERSION
from sentinel_contracts.worker_diagnostics import WorkerDiagnostics
from trading_automaton.domain.errors import CoreOperationError

_SUBMIT_RESULT = TypeAdapter(SubmitAuthorizationResult)


class CoreClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        application_environment: str | None = None,
        access_mode: str | None = None,
        worker_id: str | None = None,
        broker_id: UUID | None = None,
        worker_token: str | None = None,
        strategy_enabled: bool = False,
    ) -> None:
        self._http = http
        self._application_environment = application_environment
        self._access_mode = access_mode
        self._instance_id = uuid4()
        self._runtime_secret = secrets.token_hex(32)
        self._worker_id = worker_id
        self._broker_id = broker_id
        self._worker_token = worker_token
        self._strategy_enabled = strategy_enabled
        self._registration: RuntimeRegistrationResult | None = None
        self._owner_expired = False

    def validate_runtime(self) -> None:
        if self._application_environment is None and self._access_mode is None:
            return
        if self._application_environment == "PROD":
            self._ensure_runtime_registered()
            if self._worker_id and self._broker_id and self._worker_token:
                self._require_runtime_owner()
        response = self._request("GET", "/internal/runtime", headers=self._headers())
        configuration = response.json()
        if configuration.get("environment") != self._application_environment:
            raise ValueError("Worker/Core environment mismatch")
        if configuration.get("access_mode") != self._access_mode:
            raise ValueError("Worker/Core access mode mismatch")

    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request(
            "POST",
            "/internal/automation-commands",
            json={"worker_id": worker_id, "limit": limit},
            headers=self._headers(),
        )
        return [AutomationCommand.model_validate(item) for item in response.json()["commands"]]

    def automation_statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request(
            "POST",
            "/internal/automation-statuses",
            json={"automation_ids": [str(item) for item in automation_ids]},
            headers=self._headers(),
        )
        return AutomationStatusesResult.model_validate(response.json())

    def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
        request = FactBatchRequest(facts=facts)
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request(
            "POST",
            "/internal/automation-facts",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
        )
        return FactBatchResult.model_validate(response.json())

    def heartbeat(self, worker_id: str, occurred_at: datetime, diagnostics: WorkerDiagnostics | None = None) -> None:
        self.validate_runtime()
        self.renew_admission()
        self._request(
            "POST",
            "/internal/automaton/heartbeats",
            json={
                "worker_id": worker_id,
                "occurred_at": _json_value(occurred_at),
                **({"worker_diagnostics": diagnostics.model_dump(mode="json")} if diagnostics is not None else {}),
                **(
                    {
                        "runtime_version": {
                            "version": SERVICE_VERSION,
                            "instance_id": str(self._instance_id),
                            "environment": self._application_environment,
                            "access_mode": self._access_mode,
                        }
                    }
                    if self._application_environment is not None and self._access_mode is not None
                    else {}
                ),
            },
            headers=self._headers(),
        )

    def renew_admission(self) -> None:
        registration = self._registration
        if (
            self._application_environment != "PROD"
            or self._broker_id is None
            or registration is None
            or registration.registration_status.value != "OWNER"
        ):
            return
        response = self._request(
            "POST",
            f"/internal/automaton/brokers/{self._broker_id}/admission/renew",
            headers=self._headers(include_worker_auth=True),
        )
        try:
            renewed = RuntimeRegistrationResult.model_validate(response.json())
        except Exception as error:
            self._owner_expired = True
            raise CoreOperationError(retryable=True) from error
        if (
            renewed.registration_status.value != "OWNER"
            or renewed.runtime_instance_id != registration.runtime_instance_id
            or renewed.generation != registration.generation
        ):
            self._registration = renewed
            self._owner_expired = True
            raise CoreOperationError(retryable=True)
        self._registration = renewed

    def broker_connection(self, broker_id: str) -> BrokerConnection:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request("GET", f"/internal/automaton/brokers/{broker_id}/connection", headers=self._headers())
        return BrokerConnection(**response.json())

    def broker_scope(self, broker_id: str) -> BrokerScope:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request("GET", f"/internal/automaton/brokers/{broker_id}/scope", headers=self._headers())
        return BrokerScope.model_validate(response.json())

    @property
    def runtime_instance_id(self) -> UUID:
        return self._instance_id

    @property
    def runtime_generation(self) -> int | None:
        registration = self._registration
        if registration is None or registration.registration_status.value != "OWNER":
            return None
        return registration.generation

    def register_runtime(self, request: RuntimeRegistrationRequest) -> RuntimeRegistrationResult:
        if self._owner_expired:
            raise CoreOperationError(retryable=True)
        if request.runtime_instance_id != self._instance_id:
            raise ValueError("Runtime registration instance mismatch")
        if self._worker_id is not None and request.worker_id != self._worker_id:
            raise ValueError("Runtime registration worker mismatch")
        response = self._request(
            "POST",
            "/internal/automaton/runtime-instances",
            json=request.model_dump(mode="json"),
            headers=self._headers(include_worker_auth=True),
        )
        self._registration = RuntimeRegistrationResult.model_validate(response.json())
        return self._registration

    def _ensure_runtime_registered(self) -> None:
        if self._owner_expired:
            raise CoreOperationError(retryable=True)
        if (self._registration is not None and self._registration.registration_status.value == "OWNER") or not (
            self._worker_id and self._broker_id and self._worker_token
        ):
            return
        if self._application_environment is None or self._access_mode is None:
            raise ValueError("PROD runtime registration requires environment and access mode")
        self.register_runtime(
            RuntimeRegistrationRequest(
                worker_id=self._worker_id,
                runtime_instance_id=self._instance_id,
                broker_id=self._broker_id,
                application_environment=self._application_environment,
                access_mode=self._access_mode,
                strategy_enabled=self._strategy_enabled,
            )
        )

    def trading_admission(self, broker_id: str) -> AdmissionView:
        self.validate_runtime()
        response = self._request(
            "GET",
            f"/internal/automaton/brokers/{broker_id}/admission",
            headers=self._headers(include_worker_auth=True),
        )
        return AdmissionView.model_validate(response.json())

    def authorize_submit(self, request: SubmitAuthorizationRequest) -> SubmitAuthorizationResult:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request(
            "POST",
            f"/internal/automaton/brokers/{request.broker_id}/submit-authorizations",
            json=request.model_dump(mode="json"),
            headers=self._headers(include_worker_auth=True),
        )
        return _SUBMIT_RESULT.validate_python(response.json())

    def submit_attempt(
        self,
        broker_id: str,
        idempotency_key: str,
        request: SubmitAttemptRequest,
    ) -> SubmitAttemptView:
        self.validate_runtime()
        self._require_runtime_owner()
        response = self._request(
            "GET",
            f"/internal/automaton/brokers/{broker_id}/submit-attempts/{idempotency_key}",
            params=request.model_dump(mode="json", exclude_none=True),
            headers=self._headers(include_worker_auth=True),
        )
        return SubmitAttemptView.model_validate(response.json())

    def _require_runtime_owner(self) -> None:
        if self._application_environment == "PROD" and (
            self._registration is None or self._registration.registration_status.value != "OWNER"
        ):
            raise CoreOperationError(retryable=True)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise CoreOperationError(retryable=status >= 500, status_code=status) from error
        except httpx.TransportError as error:
            raise CoreOperationError(retryable=True) from error

    def _headers(self, *, include_worker_auth: bool = False) -> dict[str, str]:
        process_id = current_process_id()
        headers = {} if process_id is None else {"X-Process-ID": process_id}
        if self._application_environment == "PROD":
            headers.update(
                {
                    "X-Runtime-Instance-ID": str(self._instance_id),
                    "X-Runtime-Secret": self._runtime_secret,
                }
            )
        if (include_worker_auth or self._application_environment == "PROD") and self._worker_token:
            headers["Authorization"] = f"Bearer {self._worker_token}"
        return headers


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value
