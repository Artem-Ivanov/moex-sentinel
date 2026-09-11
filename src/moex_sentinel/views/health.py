"""Public health View."""

from collections.abc import Callable
from typing import Literal, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import ConfigDict, Field
from sqlalchemy import Engine

from moex_sentinel import __version__
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
    engine = cast(Engine, request.app.state.database_engine)
    checker = cast(Callable[[Engine], bool], request.app.state.database_checker)
    try:
        database_ok = checker(engine)
    except Exception:
        database_ok = False

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="error",
            version=__version__,
            database="error",
            schema="incompatible",
        )

    schema_checker = cast(Callable[[Engine], bool], request.app.state.schema_checker)
    try:
        schema_ok = schema_checker(engine)
    except Exception:
        schema_ok = False
    if not schema_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="error",
            version=__version__,
            database="ok",
            schema="incompatible",
        )
    return HealthResponse(
        status="ok",
        version=__version__,
        database="ok",
        schema="compatible",
    )
