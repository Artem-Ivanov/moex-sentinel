"""HTTP schema for scoped execution diagnostics."""

from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from moex_sentinel.domain.execution_diagnostics import ExecutionDiagnosticsView
from sentinel_contracts.base import PositionalModel


class ExecutionDiagnosticsSchema(PositionalModel):
    model_config = ConfigDict(frozen=True)

    automation_id: str
    broker_id: str
    account_id: str
    instrument_id: str
    external_instrument_id: str
    catalog_lot_size: int
    currency: str
    captured_at: datetime
    worker_observation: dict[str, Any]
    owner_runtime_instance_id: str | None
    owner_generation: int | None
    orders: tuple[dict[str, Any], ...]
    reservations: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    executions: tuple[dict[str, Any], ...]
    accepted_envelopes: tuple[dict[str, Any], ...]
    truncated: bool

    @classmethod
    def from_domain(cls, value: ExecutionDiagnosticsView) -> "ExecutionDiagnosticsSchema":
        return cls(
            automation_id=str(value.automation_id),
            broker_id=str(value.broker_id),
            account_id=value.account_id,
            instrument_id=str(value.instrument_id),
            external_instrument_id=value.external_instrument_id,
            catalog_lot_size=value.catalog_lot_size,
            currency=value.currency,
            captured_at=value.captured_at,
            worker_observation=value.worker_observation,
            owner_runtime_instance_id=(
                None if value.owner_runtime_instance_id is None else str(value.owner_runtime_instance_id)
            ),
            owner_generation=value.owner_generation,
            orders=value.orders,
            reservations=value.reservations,
            events=value.events,
            executions=value.executions,
            accepted_envelopes=value.accepted_envelopes,
            truncated=value.truncated,
        )
