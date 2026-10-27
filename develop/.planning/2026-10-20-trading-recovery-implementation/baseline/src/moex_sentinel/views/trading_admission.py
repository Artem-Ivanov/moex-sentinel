"""Operator-managed endpoints for production trading admission."""

from datetime import datetime
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from moex_sentinel.domain.daily_admission_preparation import DailyAdmissionPreparationView
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from sentinel_contracts.time import require_utc_millisecond
from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    PendingRuntimeCandidate,
    ReconciliationRequest,
    ReconciliationResult,
    RevokeRequest,
    UpdateLimitsRequest,
)

router = APIRouter(tags=["trading-admission"])


@router.get(
    "/brokers/{broker_id}/trading-admission/preparation",
    response_model=DailyAdmissionPreparationView,
)
async def view_daily_preparation(broker_id: str, request: Request) -> DailyAdmissionPreparationView | JSONResponse:
    try:
        return await _usecases(request).daily_admission_preparation.snapshot(broker_id)
    except ValueError:
        return JSONResponse(
            {"detail": "Trading admission preparation is unavailable for this account"}, status_code=404
        )


@router.get("/brokers/{broker_id}/trading-admission", response_model=AdmissionView)
async def view_admission(broker_id: str, request: Request) -> AdmissionView | JSONResponse:
    try:
        return await run_sync(partial(_usecases(request).trading_admission.view, broker_id))
    except ValueError:
        return JSONResponse({"detail": "Trading admission is unavailable for this account"}, status_code=404)


@router.get(
    "/brokers/{broker_id}/trading-admission/pending-runtime",
    response_model=PendingRuntimeCandidate,
)
async def pending_runtime_candidate(
    broker_id: str,
    request: Request,
    worker_id: Annotated[str, Query(min_length=1, max_length=128)],
    registered_after: Annotated[datetime, Query()],
) -> PendingRuntimeCandidate | JSONResponse:
    try:
        registered_after = require_utc_millisecond(registered_after)
    except ValueError:
        return JSONResponse(
            {"detail": "registered_after must be explicit UTC with millisecond precision"}, status_code=422
        )
    try:
        candidate = await run_sync(
            partial(
                _usecases(request).trading_admission.pending_runtime_candidate,
                broker_id,
                worker_id,
                registered_after,
            )
        )
        if candidate is None:
            return JSONResponse({"detail": "No pending runtime matches the requested scope"}, status_code=404)
        return candidate
    except AdmissionConflictError:
        return JSONResponse({"detail": "Multiple pending runtimes match the requested scope"}, status_code=409)
    except ValueError:
        return JSONResponse({"detail": "Pending runtime scope is unavailable"}, status_code=404)


@router.put("/brokers/{broker_id}/trading-admission/limits", response_model=AdmissionView)
async def update_limits(broker_id: str, payload: UpdateLimitsRequest, request: Request) -> AdmissionView | JSONResponse:
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.update_limits,
                broker_id,
                payload,
                actor=request.state.operator_session.username,
            )
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Admission revision changed or account is unavailable"}, status_code=409)


@router.post("/brokers/{broker_id}/trading-admission/baselines", response_model=AdmissionBaseline)
async def capture_baseline(
    broker_id: str, payload: CaptureBaselineRequest, request: Request
) -> AdmissionBaseline | JSONResponse:
    actor = request.state.operator_session.username
    try:
        return await _usecases(request).trading_admission.capture_baseline(broker_id, payload, accepted_by=actor)
    except AdmissionConflictError:
        return JSONResponse({"detail": "A baseline is already accepted for this account and day"}, status_code=409)
    except ValueError:
        return JSONResponse({"detail": "Authoritative baseline data is unavailable"}, status_code=409)


@router.post("/brokers/{broker_id}/trading-admission/reconcile", response_model=ReconciliationResult)
async def reconcile(
    broker_id: str, payload: ReconciliationRequest, request: Request
) -> ReconciliationResult | JSONResponse:
    try:
        view = await run_sync(partial(_usecases(request).trading_admission.view, broker_id))
        if view.owner_runtime_instance_id is None or view.owner_generation is None:
            return JSONResponse({"detail": "Account has no runtime owner"}, status_code=409)
        return await _usecases(request).trading_admission.reconcile(
            broker_id,
            payload,
            runtime_instance_id=view.owner_runtime_instance_id,
            generation=view.owner_generation,
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Admission revision or owner changed"}, status_code=409)
    except ValueError:
        return JSONResponse({"detail": "Authoritative account reconciliation is unavailable"}, status_code=409)


@router.post("/brokers/{broker_id}/trading-admission/arm", response_model=AdmissionView)
async def arm(broker_id: str, payload: ArmRequest, request: Request) -> AdmissionView | JSONResponse:
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.arm,
                broker_id,
                payload,
                actor=request.state.operator_session.username,
            )
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Admission prerequisites or revision changed"}, status_code=409)


@router.post("/brokers/{broker_id}/trading-admission/revoke", response_model=AdmissionView)
async def revoke(broker_id: str, payload: RevokeRequest, request: Request) -> AdmissionView | JSONResponse:
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.revoke,
                broker_id,
                payload,
                actor=request.state.operator_session.username,
            )
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Admission revision changed"}, status_code=409)


@router.post("/brokers/{broker_id}/trading-admission/owner-stopped", response_model=AdmissionView)
async def owner_stopped(broker_id: str, payload: OwnerStoppedRequest, request: Request) -> AdmissionView | JSONResponse:
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.owner_stopped,
                broker_id,
                payload,
                actor=request.state.operator_session.username,
            )
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Owner stop attestation or revision does not match"}, status_code=409)
