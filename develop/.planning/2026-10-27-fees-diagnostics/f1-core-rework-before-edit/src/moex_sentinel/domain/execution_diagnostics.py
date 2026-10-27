"""Bounded, scoped execution-history read model."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class ExecutionDiagnosticsView(PositionalModel):
    model_config = ConfigDict(frozen=True)

    automation_id: UUID
    broker_id: UUID
    account_id: str
    instrument_id: UUID
    external_instrument_id: str
    catalog_lot_size: int
    currency: str
    captured_at: datetime
    worker_observation: dict[str, Any]
    owner_runtime_instance_id: UUID | None
    owner_generation: int | None
    orders: tuple[dict[str, Any], ...]
    reservations: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    executions: tuple[dict[str, Any], ...]
    accepted_envelopes: tuple[dict[str, Any], ...]
    truncated: bool
