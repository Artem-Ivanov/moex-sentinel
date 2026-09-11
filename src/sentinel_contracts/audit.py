"""Structured audit events correlated by one business-process UUID."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

_PROCESS_ID: ContextVar[str | None] = ContextVar("process_id", default=None)
_PARENT_PROCESS_ID: ContextVar[str | None] = ContextVar("parent_process_id", default=None)
_EVENT_FIELDS = {
    "automation_id",
    "broker_id",
    "instrument_id",
    "ticker",
    "account_id",
    "worker_id",
    "reason_code",
    "data",
}


def current_process_id() -> str | None:
    return _PROCESS_ID.get()


@contextmanager
def business_process(*, process_id: str | None = None, parent_process_id: str | None = None) -> Iterator[str]:
    resolved = str(UUID(process_id)) if process_id is not None else str(uuid4())
    process_token = _PROCESS_ID.set(resolved)
    parent_token = _PARENT_PROCESS_ID.set(parent_process_id)
    try:
        yield resolved
    finally:
        _PARENT_PROCESS_ID.reset(parent_token)
        _PROCESS_ID.reset(process_token)


class JsonAuditFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        try:
            timestamp = (
                datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            event: dict[str, Any] = {
                "timestamp": timestamp,
                "level": record.levelname,
                "service": self._service,
                "process_id": current_process_id(),
                "parent_process_id": _PARENT_PROCESS_ID.get(),
                "stage": getattr(record, "stage", "APPLICATION_LOG"),
                "message": record.getMessage(),
            }
            for field in _EVENT_FIELDS:
                if hasattr(record, field):
                    event[field] = getattr(record, field)
            if record.exc_info and record.exc_info[0] is not None:
                event["exception_type"] = record.exc_info[0].__name__
            return json.dumps(event, default=_json_value, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "level": "ERROR",
                    "service": self._service,
                    "process_id": current_process_id(),
                    "stage": "LOG_FORMATTING_FAILED",
                    "message": "Audit log formatting failed",
                }
            )


class ConsoleAuditFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", "APPLICATION_LOG")
        return (
            f"{record.levelname} service={self._service} "
            f"process_id={current_process_id()} stage={stage} {record.getMessage()}"
        )


class SuccessfulHeartbeatFilter(logging.Filter):
    """Suppress only successful client heartbeat noise; failures stay visible."""

    _HEARTBEAT_PATH = "/internal/automaton/heartbeats"

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        is_successful_httpx_heartbeat = (
            record.name.startswith("httpx")
            and record.levelno == logging.INFO
            and self._HEARTBEAT_PATH in message
            and "200 OK" in message
        )
        return not is_successful_httpx_heartbeat


def configure_logging(service: str, *, level: str = "INFO", format: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SuccessfulHeartbeatFilter())
    if format.lower() == "json":
        handler.setFormatter(JsonAuditFormatter(service))
    else:
        handler.setFormatter(ConsoleAuditFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def audit_event(
    logger: logging.Logger,
    stage: str,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    allowed = {key: value for key, value in fields.items() if key in _EVENT_FIELDS}
    logger.log(level, message, extra={"stage": stage, **allowed})


def _json_value(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
