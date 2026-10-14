"""Internal Core API consumed by the independent trading worker."""

from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.automations import (
    BrokerConnectionSchema,
    HeartbeatRequestSchema,
    HeartbeatResponseSchema,
)
from sentinel_contracts.broker_execution import BrokerScope

router = APIRouter(prefix="/internal/automaton", tags=["internal-automaton"])


@router.post("/heartbeats", response_model=HeartbeatResponseSchema)
async def record_heartbeat(payload: HeartbeatRequestSchema, request: Request) -> HeartbeatResponseSchema:
    principal = getattr(request.state, "worker_principal", None)
    if principal is not None and payload.worker_id != principal.worker_id:
        return JSONResponse({"detail": "Worker identity is outside the configured scope"}, status_code=403)  # type: ignore[return-value]
    result = _usecases(request).record_automaton_heartbeat.execute(payload.worker_id, payload.occurred_at)
    if payload.runtime_version is not None:
        request.app.state.worker_versions.observe(
            payload.runtime_version, payload.occurred_at, payload.worker_diagnostics
        )
    return HeartbeatResponseSchema.model_validate(result, from_attributes=True)


@router.get("/brokers/{broker_id}/connection", response_model=BrokerConnectionSchema)
async def broker_connection(broker_id: str, request: Request) -> BrokerConnectionSchema:
    principal = getattr(request.state, "worker_principal", None)
    if principal is not None and str(principal.broker_id) != broker_id:
        return JSONResponse({"detail": "Broker scope is unavailable"}, status_code=404)  # type: ignore[return-value]
    return BrokerConnectionSchema.from_domain(
        await run_sync(partial(_usecases(request).view_automaton_broker_connection.execute, broker_id))
    )


@router.get("/brokers/{broker_id}/scope", response_model=BrokerScope)
async def broker_scope(broker_id: str, request: Request) -> BrokerScope:
    principal = getattr(request.state, "worker_principal", None)
    if principal is not None and str(principal.broker_id) != broker_id:
        return JSONResponse({"detail": "Broker scope is unavailable"}, status_code=404)  # type: ignore[return-value]
    return await run_sync(partial(_usecases(request).view_automaton_broker_scope.execute, broker_id))
