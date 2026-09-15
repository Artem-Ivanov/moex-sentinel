"""Public health View."""

from typing import Literal, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import ConfigDict, Field

from moex_sentinel import __version__
from moex_sentinel.usecases.health import CheckReadinessUsecase
from sentinel_contracts.base import PositionalModel

router = APIRouter()


class HealthResponse(PositionalModel):
    """Stable public health response."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    status: Literal["ok", "error"]
    version: str
    database: Literal["ok", "error"]
    schema_status: Literal["compatible", "incompatible"] = Field(alias="schema")
    service: Literal["backend"] = "backend"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, response: Response) -> HealthResponse:
    """Report HTTP and database readiness without leaking connection errors."""
    usecase = cast(CheckReadinessUsecase, request.app.state.readiness_usecase)
    readiness = usecase.execute()
    ready = readiness.database_ok and readiness.schema_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if ready else "error",
        version=__version__,
        database="ok" if readiness.database_ok else "error",
        schema="compatible" if readiness.schema_ok else "incompatible",
    )
