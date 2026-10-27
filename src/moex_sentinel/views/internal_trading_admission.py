"""Internal worker registration and admission API."""

from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from moex_sentinel.api.worker_auth import WorkerAuthenticationError
from moex_sentinel.domain.trading_admission import AdmissionConflictError
from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from sentinel_contracts.trading_admission import (
    AdmissionView,
    RuntimeRegistrationRequest,
    RuntimeRegistrationResult,
    SubmitAttemptRequest,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
)

router = APIRouter(tags=["internal-trading-admission"])


def _submit_attempt_origin(
    original_runtime_instance_id: UUID | None = None,
    original_generation: Annotated[str | None, Query(pattern=r"^[0-9]+$")] = None,
) -> SubmitAttemptRequest:
    try:
        return SubmitAttemptRequest(
            original_runtime_instance_id=original_runtime_instance_id,
            original_generation=None if original_generation is None else int(original_generation),
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail="Invalid submit attempt origin") from error


@router.get("/internal/automaton/brokers/{broker_id}/admission", response_model=AdmissionView)
async def view_worker_admission(broker_id: str, request: Request) -> AdmissionView | JSONResponse:
    principal = request.state.worker_principal
    if str(principal.broker_id) != broker_id:
        return JSONResponse({"detail": "Broker scope is unavailable"}, status_code=404)
    try:
        return await run_sync(partial(_usecases(request).trading_admission.view, broker_id))
    except ValueError:
        return JSONResponse({"detail": "Trading admission is unavailable"}, status_code=404)


@router.post("/internal/automaton/brokers/{broker_id}/admission/renew", response_model=RuntimeRegistrationResult)
async def renew_worker_admission(broker_id: str, request: Request) -> RuntimeRegistrationResult | JSONResponse:
    principal = request.state.worker_principal
    if str(principal.broker_id) != broker_id or principal.generation is None:
        return JSONResponse({"detail": "Runtime owner is unavailable"}, status_code=409)
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.renew_owner,
                principal.runtime_instance_id,
                principal.generation,
            )
        )
    except AdmissionConflictError:
        return JSONResponse({"detail": "Runtime owner lease cannot be renewed"}, status_code=409)


@router.post(
    "/internal/automaton/brokers/{broker_id}/submit-authorizations",
    response_model=SubmitAuthorizationResult,
)
async def authorize_submit(
    broker_id: str, payload: SubmitAuthorizationRequest, request: Request
) -> SubmitAuthorizationResult | JSONResponse:
    principal = request.state.worker_principal
    if str(principal.broker_id) != broker_id or str(payload.broker_id) != broker_id or principal.generation is None:
        return JSONResponse({"detail": "Submit account scope is unavailable"}, status_code=404)
    return await _usecases(request).trading_admission.authorize_submit(
        payload,
        runtime_instance_id=principal.runtime_instance_id,
        generation=principal.generation,
    )


@router.get(
    "/internal/automaton/brokers/{broker_id}/submit-attempts/{idempotency_key}",
    response_model=SubmitAttemptView,
)
async def submit_attempt(
    broker_id: str,
    idempotency_key: str,
    request: Request,
    origin: Annotated[SubmitAttemptRequest, Depends(_submit_attempt_origin)],
) -> SubmitAttemptView | JSONResponse:
    principal = request.state.worker_principal
    if str(principal.broker_id) != broker_id:
        return JSONResponse({"detail": "Submit account scope is unavailable"}, status_code=404)
    try:
        return await run_sync(
            partial(
                _usecases(request).trading_admission.submit_attempt,
                broker_id,
                idempotency_key,
                original_runtime_instance_id=origin.original_runtime_instance_id,
                generation=origin.original_generation,
            )
        )
    except ValueError:
        return JSONResponse({"detail": "Submit attempt is unavailable"}, status_code=404)


@router.post("/internal/automaton/runtime-instances", response_model=RuntimeRegistrationResult)
async def register_runtime(
    payload: RuntimeRegistrationRequest, request: Request
) -> RuntimeRegistrationResult | JSONResponse:
    """Register the configured worker process and bind its one-time runtime secret."""
    runtime_secret = request.headers.get("X-Runtime-Secret")
    if request.headers.get("X-Runtime-Instance-ID") != str(payload.runtime_instance_id) or runtime_secret is None:
        return JSONResponse({"detail": "Worker registration is outside the configured scope"}, status_code=403)
    try:
        request.app.state.worker_authenticator.authorize_registration(
            worker_id=payload.worker_id,
            broker_id=payload.broker_id,
            environment=payload.application_environment,
            access_mode=payload.access_mode,
            strategy_enabled=payload.strategy_enabled,
        )
        return await run_sync(partial(_usecases(request).trading_admission.register_runtime, payload, runtime_secret))
    except (WorkerAuthenticationError, ValueError):
        return JSONResponse({"detail": "Worker registration is outside the configured scope"}, status_code=403)
