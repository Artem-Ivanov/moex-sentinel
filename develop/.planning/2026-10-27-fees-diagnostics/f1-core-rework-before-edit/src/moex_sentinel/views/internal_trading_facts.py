"""Internal typed API consumed by the independent trading Worker."""

from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.trading_facts import (
    AutomationStatusesRequestSchema,
    ClaimAutomationCommandsRequestSchema,
    ClaimAutomationCommandsResponseSchema,
    PublishFactsRequestSchema,
)
from sentinel_contracts.trading_facts import AutomationStatusesResult, FactBatchResult

router = APIRouter(tags=["internal-trading-facts"])


@router.post("/internal/automation-commands", response_model=ClaimAutomationCommandsResponseSchema)
async def claim_automation_commands(
    payload: ClaimAutomationCommandsRequestSchema,
    request: Request,
) -> ClaimAutomationCommandsResponseSchema:
    principal = getattr(request.state, "worker_principal", None)
    commands = await run_sync(
        partial(
            _usecases(request).claim_automation_commands.execute,
            payload.limit,
            **({} if principal is None else {"broker_id": principal.broker_id}),
        )
    )
    return ClaimAutomationCommandsResponseSchema(commands=commands)


@router.post("/internal/automation-statuses", response_model=AutomationStatusesResult)
async def automation_statuses(
    payload: AutomationStatusesRequestSchema,
    request: Request,
) -> AutomationStatusesResult:
    principal = getattr(request.state, "worker_principal", None)
    return await run_sync(
        partial(
            _usecases(request).view_automation_statuses.execute,
            payload.automation_ids,
            commission_order_ids=payload.commission_intent_ids,
            **({} if principal is None else {"broker_id": principal.broker_id}),
        )
    )


@router.post("/internal/automation-facts", response_model=FactBatchResult)
async def publish_automation_facts(
    payload: PublishFactsRequestSchema,
    request: Request,
) -> FactBatchResult:
    principal = getattr(request.state, "worker_principal", None)
    if principal is not None and any(fact.user_broker_id != principal.broker_id for fact in payload.facts):
        return JSONResponse({"detail": "Fact scope is unavailable"}, status_code=403)  # type: ignore[return-value]
    return await run_sync(
        partial(
            _usecases(request).publish_trading_facts.execute,
            payload.facts,
            **(
                {}
                if principal is None
                else {
                    "runtime_instance_id": principal.runtime_instance_id,
                    "generation": principal.generation,
                }
            ),
        )
    )
