"""Internal Core API consumed by the independent trading worker."""

from fastapi import APIRouter, Request

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.automations import (
    BrokerConnectionSchema,
    HeartbeatRequestSchema,
    HeartbeatResponseSchema,
)

router = APIRouter(prefix="/internal/automaton", tags=["internal-automaton"])


@router.post("/heartbeats", response_model=HeartbeatResponseSchema)
async def record_heartbeat(payload: HeartbeatRequestSchema, request: Request) -> HeartbeatResponseSchema:
    result = _usecases(request).record_automaton_heartbeat.execute(payload.worker_id, payload.occurred_at)
    return HeartbeatResponseSchema.model_validate(result, from_attributes=True)


@router.get("/brokers/{broker_id}/connection", response_model=BrokerConnectionSchema)
async def broker_connection(broker_id: str, request: Request) -> BrokerConnectionSchema:
    return BrokerConnectionSchema.from_domain(_usecases(request).view_automaton_broker_connection.execute(broker_id))
