"""Thin HTTP views for trading automation user actions."""

from functools import partial
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.automations import (
    AutomationListSchema,
    AutomationSchema,
    CreateAutomationSchema,
    DecisionReasonsSchema,
    TradingAutomationDetailsSchema,
    TradingSessionsStatusSchema,
    UpdateAutomationSettingsSchema,
)
from moex_sentinel.views.schemas.execution_diagnostics import ExecutionDiagnosticsSchema

router = APIRouter(tags=["trading-automations"])


@router.post(
    "/instruments/{broker_id}/{instrument_id}/trade",
    response_model=AutomationSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_automation(
    broker_id: str,
    instrument_id: str,
    payload: CreateAutomationSchema,
    request: Request,
) -> AutomationSchema:
    result = await _usecases(request).create_trading_automation.execute(
        broker_id,
        payload.account_id,
        instrument_id,
    )
    return AutomationSchema.from_domain(result)


@router.get("/trading-automations", response_model=AutomationListSchema)
async def list_automations(request: Request) -> AutomationListSchema:
    records = await run_sync(partial(_usecases(request).view_trading_automations.execute))
    return AutomationListSchema(items=[AutomationSchema.from_domain(item) for item in records])


@router.get("/trading-sessions/status", response_model=TradingSessionsStatusSchema)
async def trading_sessions_status(request: Request) -> TradingSessionsStatusSchema:
    result = await _usecases(request).view_trading_sessions_status.execute()
    return TradingSessionsStatusSchema.model_validate(result, from_attributes=True)


@router.get("/trading-automations/{automation_id}", response_model=AutomationSchema)
async def view_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(
        await run_sync(partial(_usecases(request).view_trading_automation.execute, automation_id))
    )


@router.get(
    "/trading-automations/{automation_id}/details",
    response_model=TradingAutomationDetailsSchema,
)
async def view_automation_details(automation_id: str, request: Request) -> TradingAutomationDetailsSchema:
    return TradingAutomationDetailsSchema.from_domain(
        await _usecases(request).view_trading_automation_details.execute(automation_id)
    )


@router.get(
    "/trading-automations/{automation_id}/execution-diagnostics",
    response_model=ExecutionDiagnosticsSchema,
)
async def view_execution_diagnostics(
    automation_id: str,
    request: Request,
    response: Response,
    limit: int = Query(default=20, ge=1, le=20),
    intent_id: UUID | None = None,
) -> ExecutionDiagnosticsSchema:
    result = await run_sync(
        partial(
            _usecases(request).view_execution_diagnostics.execute,
            automation_id,
            limit=limit,
            intent_id=intent_id,
            worker_store=request.app.state.worker_versions,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return ExecutionDiagnosticsSchema.from_domain(result)


@router.get(
    "/trading-automations/{automation_id}/decision-reasons",
    response_model=DecisionReasonsSchema,
)
async def view_automation_decision_reasons(
    automation_id: str,
    request: Request,
    limit: int = Query(default=5, ge=1, le=20),
) -> DecisionReasonsSchema:
    result = await run_sync(
        partial(_usecases(request).view_trading_automation_decision_reasons.execute, automation_id, limit)
    )
    return DecisionReasonsSchema.model_validate(result, from_attributes=True)


@router.post("/trading-automations/{automation_id}/hold", response_model=AutomationSchema)
async def hold_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(
        await run_sync(partial(_usecases(request).hold_automation.execute, automation_id))
    )


@router.post("/trading-automations/{automation_id}/resume", response_model=AutomationSchema)
async def resume_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(
        await run_sync(partial(_usecases(request).resume_automation.execute, automation_id))
    )


@router.post("/trading-automations/{automation_id}/close", response_model=AutomationSchema)
async def close_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(
        await run_sync(partial(_usecases(request).close_automation.execute, automation_id))
    )


@router.patch("/trading-automations/{automation_id}/settings", response_model=AutomationSchema)
async def update_automation_settings(
    automation_id: str,
    payload: UpdateAutomationSettingsSchema,
    request: Request,
) -> AutomationSchema:
    result = await run_sync(
        partial(
            _usecases(request).update_trading_automation_settings.execute,
            automation_id,
            expected_revision=payload.expected_revision,
            expected_settings_revision=payload.expected_settings_revision,
            order_quota_percent=payload.order_quota_percent,
        )
    )
    return AutomationSchema.from_domain(result)
