"""Shared HTTP rendering for application errors."""

from typing import cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from moex_sentinel.usecases.errors import UseCaseError


async def render_usecase_error(_request: Request, exception: Exception) -> JSONResponse:
    error = cast(UseCaseError, exception)
    status_code = (
        404
        if error.code
        in {
            "AUTOMATION_NOT_FOUND",
            "BROKER_NOT_FOUND",
            "INSTRUMENT_NOT_FOUND",
            "POSITION_NOT_FOUND",
        }
        else 409
    )
    if error.code in {"UNKNOWN_ADAPTER", "INVALID_BROKER_FIELDS"}:
        status_code = 422
    if error.code in {
        "INVALID_BROKER_CONFIGURATION",
        "INVALID_CANDLE_RANGE",
        "INVALID_ENVIRONMENT",
        "INVALID_INSTRUMENT_CATEGORY",
        "INVALID_MARKET_QUERY",
        "INVALID_AUTOMATION_ENTRY_BUDGET",
    }:
        status_code = 422
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": error.code,
                "message": error.message,
                "fields": [{"path": item.path, "code": item.code, "message": item.message} for item in error.fields],
            }
        },
    )


async def render_validation_error(request: Request, exception: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exception)
    try:
        body = await request.json()
    except Exception:
        body = None

    fields = []
    for item in error.errors():
        location = tuple(part for part in item["loc"] if part != "body")
        path = _validation_path(location, body)
        error_type = str(item["type"]).upper()
        message = "Заполните обязательное поле." if item["type"] == "missing" else "Проверьте значение поля."
        fields.append({"path": path, "code": error_type, "message": message})
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "INVALID_REQUEST",
                "message": "Проверьте данные формы.",
                "fields": fields,
            }
        },
    )


def _validation_path(location: tuple[object, ...], body: object) -> str:
    if len(location) >= 3 and location[0] == "fields" and isinstance(location[1], int) and isinstance(body, dict):
        submitted_fields = body.get("fields")
        if isinstance(submitted_fields, list) and location[1] < len(submitted_fields):
            submitted = submitted_fields[location[1]]
            if isinstance(submitted, dict) and isinstance(submitted.get("name"), str):
                return f"fields.{submitted['name']}"
    return ".".join(str(part) for part in location if part != "value")
