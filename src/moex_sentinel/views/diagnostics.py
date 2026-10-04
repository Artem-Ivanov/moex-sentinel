"""Authenticated diagnostic snapshot for the operator."""

from functools import partial
from typing import cast

from fastapi import APIRouter, Request, Response

from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.usecases.diagnostics import DiagnosticsStatus, GetDiagnosticsStatusUsecase

router = APIRouter(tags=["diagnostics"])


@router.get("/diagnostics/status", response_model=DiagnosticsStatus)
async def diagnostics_status(request: Request, response: Response) -> DiagnosticsStatus:
    """Run synchronous readiness in the app-owned executor, without caching."""
    response.headers["Cache-Control"] = "no-store"
    usecase = cast(GetDiagnosticsStatusUsecase, request.app.state.diagnostics_usecase)
    analytics = await request.app.state.analytics_versions.observe()
    return await run_sync(partial(usecase.execute, analytics))
