"""HTTP adapter for the Core internal automaton contract."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

import httpx

from sentinel_contracts.audit import current_process_id
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading_facts import (
    AutomationCommand,
    AutomationStatusesResult,
    FactBatchRequest,
    FactBatchResult,
    FactEnvelope,
)


class CoreClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
        response = self._http.post(
            "/internal/automation-commands",
            json={"worker_id": worker_id, "limit": limit},
            headers=self._headers(),
        )
        response.raise_for_status()
        return [AutomationCommand.model_validate(item) for item in response.json()["commands"]]

    def automation_statuses(self, automation_ids: list[UUID]) -> AutomationStatusesResult:
        response = self._http.post(
            "/internal/automation-statuses",
            json={"automation_ids": [str(item) for item in automation_ids]},
            headers=self._headers(),
        )
        response.raise_for_status()
        return AutomationStatusesResult.model_validate(response.json())

    def publish_facts(self, facts: list[FactEnvelope]) -> FactBatchResult:
        request = FactBatchRequest(facts=facts)
        response = self._http.post(
            "/internal/automation-facts",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
        )
        response.raise_for_status()
        return FactBatchResult.model_validate(response.json())

    def heartbeat(self, worker_id: str, occurred_at: datetime) -> None:
        response = self._http.post(
            "/internal/automaton/heartbeats",
            json={"worker_id": worker_id, "occurred_at": _json_value(occurred_at)},
            headers=self._headers(),
        )
        response.raise_for_status()

    def broker_connection(self, broker_id: str) -> BrokerConnection:
        response = self._http.get(f"/internal/automaton/brokers/{broker_id}/connection", headers=self._headers())
        response.raise_for_status()
        return BrokerConnection(**response.json())

    @staticmethod
    def _headers() -> dict[str, str]:
        process_id = current_process_id()
        return {} if process_id is None else {"X-Process-ID": process_id}


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
