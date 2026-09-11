"""Internal typed API consumed by the independent trading Worker."""

from fastapi import APIRouter, Request

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
    commands = _usecases(request).claim_automation_commands.execute(payload.limit)
    return ClaimAutomationCommandsResponseSchema(commands=commands)


@router.post("/internal/automation-statuses", response_model=AutomationStatusesResult)
async def automation_statuses(
    payload: AutomationStatusesRequestSchema,
    request: Request,
) -> AutomationStatusesResult:
    return _usecases(request).view_automation_statuses.execute(payload.automation_ids)


@router.post("/internal/automation-facts", response_model=FactBatchResult)
async def publish_automation_facts(
    payload: PublishFactsRequestSchema,
    request: Request,
) -> FactBatchResult:
    return _usecases(request).publish_trading_facts.execute(payload.facts)
