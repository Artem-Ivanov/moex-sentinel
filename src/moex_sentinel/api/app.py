"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from moex_sentinel import __version__
from moex_sentinel.config import Settings
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.schema_revision import expected_schema_revision, schema_is_compatible
from moex_sentinel.usecases.errors import UseCaseError
from moex_sentinel.views.automations import router as automation_router
from moex_sentinel.views.brokers import router as broker_router
from moex_sentinel.views.errors import render_usecase_error, render_validation_error
from moex_sentinel.views.health import router as health_router
from moex_sentinel.views.instruments import router as instrument_router
from moex_sentinel.views.internal_automaton import router as internal_automaton_router
from moex_sentinel.views.internal_market import router as internal_market_router
from moex_sentinel.views.internal_trading_facts import router as internal_trading_facts_router
from moex_sentinel.views.market_data import router as market_data_router
from moex_sentinel.views.portfolio import router as portfolio_router
from moex_sentinel.views.trading_summary import router as trading_summary_router
from sentinel_contracts.audit import (
    audit_event,
    business_process,
    configure_logging,
)

LOGGER = logging.getLogger(__name__)


def build_application_usecases(factory: object) -> object:
    """Load concrete dependencies only when the application lifespan starts."""
    from moex_sentinel.composition import build_application_usecases as build  # noqa: PLC0415

    return build(factory)  # type: ignore[arg-type]


def check_database(engine: Engine) -> bool:
    """Execute a minimal database readiness query."""
    with engine.connect() as connection:
        return cast(int, connection.scalar(text("SELECT 1"))) == 1


def build_market_snapshot_gateway(factory: object, *, retry_limit: int = 5) -> object:
    """Construct the lazy market source owner without opening a broker connection."""
    from moex_sentinel.composition import build_market_snapshot_gateway as build  # noqa: PLC0415

    return build(factory, retry_limit=retry_limit)  # type: ignore[arg-type]


def check_schema(engine: Engine) -> bool:
    """Require Alembic head on PostgreSQL while preserving lightweight SQLite tests."""
    if engine.dialect.name == "sqlite":
        return True
    return schema_is_compatible(engine, expected_schema_revision())


def create_app(
    *,
    database_url: str | URL | None = None,
    database_checker: Callable[[Engine], bool] = check_database,
    schema_checker: Callable[[Engine], bool] = check_schema,
    configure_audit: bool = False,
) -> FastAPI:
    """Create a configured HTTP application."""
    settings = Settings()
    configured_url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if configure_audit:
            configure_logging("backend", level=settings.log_level, format=settings.log_format)
        engine = create_database_engine(configured_url)
        application.state.database_engine = engine
        application.state.session_factory = create_session_factory(engine)
        application.state.usecases = build_application_usecases(application.state.session_factory)
        application.state.market_snapshot_gateway = build_market_snapshot_gateway(
            application.state.session_factory, retry_limit=settings.sandbox_retry_limit
        )
        application.state.database_checker = database_checker
        application.state.schema_checker = schema_checker
        with business_process():
            audit_event(LOGGER, "APPLICATION_STARTED", "Backend application started")
        try:
            yield
        finally:
            try:
                await application.state.market_snapshot_gateway.close()
            finally:
                with business_process():
                    audit_event(LOGGER, "APPLICATION_STOPPED", "Backend application stopped")
                engine.dispose()

    application = FastAPI(
        title="MOEX Sentinel API",
        version=__version__,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def correlate_business_process(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        quiet_heartbeat = request.url.path == "/internal/automaton/heartbeats"
        supplied = request.headers.get("X-Process-ID")
        try:
            inherited = None if supplied is None else str(UUID(supplied))
        except ValueError:
            inherited = None
        with business_process(process_id=inherited) as process_id:
            audit_event(
                LOGGER,
                "HTTP_REQUEST_STARTED",
                "HTTP request started",
                level=logging.DEBUG if quiet_heartbeat else logging.INFO,
                data={"method": request.method, "path": request.url.path},
            )
            try:
                response = await call_next(request)
            except Exception as error:
                audit_event(
                    LOGGER,
                    "HTTP_REQUEST_FAILED",
                    "HTTP request failed",
                    level=logging.ERROR,
                    data={
                        "method": request.method,
                        "path": request.url.path,
                        "exception_type": type(error).__name__,
                    },
                )
                raise
            response.headers["X-Process-ID"] = process_id
            audit_event(
                LOGGER,
                "HTTP_REQUEST_COMPLETED",
                "HTTP request completed",
                level=(logging.DEBUG if quiet_heartbeat and response.status_code < 400 else logging.INFO),
                data={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                },
            )
            return response

    application.add_exception_handler(UseCaseError, render_usecase_error)
    application.add_exception_handler(RequestValidationError, render_validation_error)
    application.include_router(health_router, prefix="/api")
    application.include_router(broker_router, prefix="/api")
    application.include_router(portfolio_router, prefix="/api")
    application.include_router(trading_summary_router, prefix="/api")
    application.include_router(market_data_router, prefix="/api")
    application.include_router(instrument_router, prefix="/api")
    application.include_router(automation_router, prefix="/api")
    application.include_router(internal_automaton_router)
    application.include_router(internal_trading_facts_router)
    application.include_router(internal_market_router)
    return application


app = create_app(configure_audit=True)
