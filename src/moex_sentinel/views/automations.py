"""Thin HTTP views for trading automation user actions."""

from fastapi import APIRouter, Request, status

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.automations import (
    AutomationListSchema,
    AutomationSchema,
    CreateAutomationSchema,
    TradingAutomationDetailsSchema,
    TradingSessionsStatusSchema,
)

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
    records = _usecases(request).view_trading_automations.execute()
    return AutomationListSchema(items=[AutomationSchema.from_domain(item) for item in records])


@router.get("/trading-sessions/status", response_model=TradingSessionsStatusSchema)
async def trading_sessions_status(request: Request) -> TradingSessionsStatusSchema:
    result = await _usecases(request).view_trading_sessions_status.execute()
    return TradingSessionsStatusSchema.model_validate(result, from_attributes=True)


@router.get("/trading-automations/{automation_id}", response_model=AutomationSchema)
async def view_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(_usecases(request).view_trading_automation.execute(automation_id))


@router.get(
    "/trading-automations/{automation_id}/details",
    response_model=TradingAutomationDetailsSchema,
)
async def view_automation_details(automation_id: str, request: Request) -> TradingAutomationDetailsSchema:
    return TradingAutomationDetailsSchema.from_domain(
        await _usecases(request).view_trading_automation_details.execute(automation_id)
    )


@router.post("/trading-automations/{automation_id}/hold", response_model=AutomationSchema)
async def hold_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(_usecases(request).hold_automation.execute(automation_id))


@router.post("/trading-automations/{automation_id}/resume", response_model=AutomationSchema)
async def resume_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(_usecases(request).resume_automation.execute(automation_id))


@router.post("/trading-automations/{automation_id}/close", response_model=AutomationSchema)
async def close_automation(automation_id: str, request: Request) -> AutomationSchema:
    return AutomationSchema.from_domain(_usecases(request).close_automation.execute(automation_id))
