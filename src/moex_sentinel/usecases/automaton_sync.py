"""Heartbeat use case exposed only to the trading Worker."""

from datetime import datetime
from typing import Protocol


class AutomatonHeartbeatServicePort(Protocol):
    def record_heartbeat(self, worker_id: str, occurred_at: datetime) -> object: ...


class RecordAutomatonHeartbeatUsecase:
    def __init__(self, service: AutomatonHeartbeatServicePort) -> None:
        self._service = service

    def execute(self, worker_id: str, occurred_at: datetime) -> object:
        return self._service.record_heartbeat(worker_id, occurred_at)
