"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from hmac import compare_digest
from typing import TYPE_CHECKING, cast
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from moex_sentinel import __version__
from moex_sentinel.adapters.analytics_version import AnalyticsVersionClient
from moex_sentinel.adapters.telegram_alerts import create_telegram_alert_transport
from moex_sentinel.api.auth import OperatorAuth
from moex_sentinel.api.sync_execution import SyncExecutor
from moex_sentinel.api.worker_auth import WorkerAuthenticationError, WorkerAuthenticator
from moex_sentinel.config import Settings
from moex_sentinel.domain.daily_admission_preparation import (
    DailyAdmissionPreparationReason,
    DailyAdmissionPreparationStatus,
)
from moex_sentinel.services.runtime_versions import WorkerVersionStore
from moex_sentinel.services.sync_execution import bind_sync_runner, run_sync
from moex_sentinel.services.trading_readiness_alerts import ReadinessObservation, TradingReadinessAlertService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.schema_revision import expected_schema_revision, schema_is_compatible
from moex_sentinel.usecases.diagnostics import GetDiagnosticsStatusUsecase
from moex_sentinel.usecases.errors import UseCaseError
from moex_sentinel.usecases.health import CheckReadinessUsecase
from moex_sentinel.usecases.market_snapshot import GetMarketSnapshotUsecase
from moex_sentinel.views.automations import router as automation_router
from moex_sentinel.views.brokers import router as broker_router
from moex_sentinel.views.diagnostics import router as diagnostics_router
from moex_sentinel.views.errors import render_usecase_error, render_validation_error
from moex_sentinel.views.health import router as health_router
from moex_sentinel.views.instruments import router as instrument_router
from moex_sentinel.views.internal_automaton import router as internal_automaton_router
from moex_sentinel.views.internal_market import router as internal_market_router
from moex_sentinel.views.internal_trading_admission import router as internal_trading_admission_router
from moex_sentinel.views.internal_trading_facts import router as internal_trading_facts_router
from moex_sentinel.views.market_data import router as market_data_router
from moex_sentinel.views.portfolio import router as portfolio_router
from moex_sentinel.views.trading_admission import router as trading_admission_router
from moex_sentinel.views.trading_summary import router as trading_summary_router
from sentinel_contracts.audit import (
    audit_event,
    business_process,
    configure_logging,
)
from sentinel_contracts.time import utc_now_ms

LOGGER = logging.getLogger(__name__)


def _preparation_alert_fields(preparation):
    if preparation.status is DailyAdmissionPreparationStatus.PREPARED:
        return "READY", "ARM_REQUIRED"
    if preparation.status in {
        DailyAdmissionPreparationStatus.DISABLED,
        DailyAdmissionPreparationStatus.WAITING,
    }:
        reason = preparation.reason
        return "UNKNOWN", "NOT_OBSERVED" if reason is DailyAdmissionPreparationReason.NOT_OBSERVED else "UNKNOWN"
    reason_map = {
        DailyAdmissionPreparationReason.OWNER_MISSING: "OWNER_MISSING",
        DailyAdmissionPreparationReason.OWNER_MISMATCH: "OWNER_MISMATCH",
        DailyAdmissionPreparationReason.LEASE_EXPIRED: "LEASE_EXPIRED",
        DailyAdmissionPreparationReason.BASELINE_REQUIRED: "BASELINE_MISSING",
        DailyAdmissionPreparationReason.RECONCILIATION_REQUIRED: "RECONCILIATION_REQUIRED",
        DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED: "RECONCILIATION_REQUIRED",
        DailyAdmissionPreparationReason.ARM_REQUIRED: "ARM_REQUIRED",
        DailyAdmissionPreparationReason.CORE_UNAVAILABLE: "CORE_UNAVAILABLE",
        DailyAdmissionPreparationReason.SNAPSHOT_INVALID: "MARKET_STALE",
        DailyAdmissionPreparationReason.SNAPSHOT_EXPIRED: "MARKET_STALE",
        DailyAdmissionPreparationReason.MOSCOW_DAY_CHANGED: "BASELINE_MISSING",
        DailyAdmissionPreparationReason.RISK_DATA_UNKNOWN: "MARKET_STALE",
    }
    if preparation.reason is DailyAdmissionPreparationReason.RECOVERY_REQUIRED:
        return "PENDING", "ARM_REQUIRED"
    if preparation.reason is DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE:
        return "UNKNOWN", "UNKNOWN"
    reason = reason_map.get(preparation.reason, "UNKNOWN")
    return ("FAILED", reason) if reason != "UNKNOWN" else ("UNKNOWN", reason)


def _build_readiness_observer(usecases, worker_versions, broker_id: str | None):
    async def observe() -> ReadinessObservation:
        _, worker = worker_versions.diagnostics_snapshot()
        now = utc_now_ms()
        if broker_id is None:
            return ReadinessObservation(
                captured_at=now,
                worker=worker,
                account_status="UNKNOWN",
                account_effective_can_submit=None,
                recovery_only=False,
                preparation_status="UNKNOWN",
                preparation_reason="OWNER_MISSING",
            )
        try:
            account = await run_sync(partial(usecases.trading_admission.view, broker_id))
        except Exception:
            account = None
        try:
            preparation = await usecases.daily_admission_preparation.snapshot(broker_id)
        except Exception:
            preparation = None
        if account is None:
            account_status = "UNKNOWN"
            effective = None
            recovery_only = False
        else:
            account_status = account.admission_status.value
            effective = account.effective_can_submit
            recovery_only = account.recovery_only
        if preparation is None:
            prep_status, prep_reason = "FAILED", "CORE_UNAVAILABLE"
        else:
            prep_status, prep_reason = _preparation_alert_fields(preparation)
            if (
                account is not None
                and account.admission_status.value == "ARMED"
                and account.effective_can_submit is True
                and preparation.reason is DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED
            ):
                prep_status, prep_reason = "PENDING", "ARM_REQUIRED"
        return ReadinessObservation(
            captured_at=now,
            worker=worker,
            account_status=account_status,
            account_effective_can_submit=effective,
            recovery_only=recovery_only,
            preparation_status=prep_status,
            preparation_reason=prep_reason,
        )

    return observe


if TYPE_CHECKING:
    from moex_sentinel.services.market_snapshot_gateway import MarketSnapshotGateway


def build_application_usecases(factory: object, *, settings: Settings | None = None) -> object:
    """Load concrete dependencies only when the application lifespan starts."""
    from moex_sentinel.composition import build_application_usecases as build  # noqa: PLC0415

    return build(factory, settings=settings)  # type: ignore[arg-type]


def check_database(engine: Engine) -> bool:
    """Execute a minimal database readiness query."""
    with engine.connect() as connection:
        return cast(int, connection.scalar(text("SELECT 1"))) == 1


def build_market_snapshot_gateway(
    factory: object, *, retry_limit: int = 5, settings: Settings | None = None
) -> "MarketSnapshotGateway":
    """Construct the lazy market source owner without opening a broker connection."""
    from moex_sentinel.composition import build_market_snapshot_gateway as build  # noqa: PLC0415

    return build(factory, retry_limit=retry_limit, settings=settings)  # type: ignore[arg-type]


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
    test_auth_bypass: bool = False,
) -> FastAPI:
    """Create a configured HTTP application."""
    settings = Settings()
    configured_url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if not test_auth_bypass:
            application.state.operator_auth = OperatorAuth(
                settings.auth_username,
                settings.auth_password_hash,
                insecure_loopback=settings.auth_insecure_loopback,
                session_cookie_name=settings.auth_session_cookie_name,
                allowed_origin=settings.auth_allowed_origin,
            )
        if configure_audit:
            configure_logging("backend", level=settings.log_level, format=settings.log_format)
        engine = create_database_engine(
            configured_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
        )
        application.state.database_engine = engine
        executor = None
        market_gateway = None
        analytics_http = None
        background_tasks: list[asyncio.Task[None]] = []
        readiness_alert_service = None
        try:
            executor = SyncExecutor(capacity=4)
            application.state.sync_executor = executor
            application.state.session_factory = create_session_factory(engine)
            application.state.usecases = build_application_usecases(
                application.state.session_factory, settings=settings
            )
            application.state.worker_authenticator = getattr(
                application.state.usecases,
                "worker_authenticator",
                WorkerAuthenticator(settings, lambda _runtime_id: None),
            )
            market_gateway = build_market_snapshot_gateway(
                application.state.session_factory, retry_limit=settings.sandbox_retry_limit, settings=settings
            )
            application.state.market_snapshot_usecase = GetMarketSnapshotUsecase(market_gateway)
            application.state.readiness_usecase = CheckReadinessUsecase(
                partial(database_checker, engine), partial(schema_checker, engine)
            )
            application.state.worker_versions = WorkerVersionStore(
                settings.application_environment, settings.broker_access_mode
            )
            analytics_http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            application.state.analytics_versions = AnalyticsVersionClient(
                settings.analytics_url,
                analytics_http,
                timeout_seconds=settings.analytics_version_http_timeout_seconds,
            )
            application.state.diagnostics_usecase = GetDiagnosticsStatusUsecase(
                application.state.readiness_usecase,
                settings.application_environment,
                settings.broker_access_mode,
                worker_observations=application.state.worker_versions.diagnostics_snapshot,
            )
            application.state.daily_admission_preparation = application.state.usecases.daily_admission_preparation
            alert_transport = (
                create_telegram_alert_transport(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    run_sync=executor.run,
                    request_timeout_seconds=settings.telegram_request_timeout_seconds,
                )
                if settings.telegram_alerts_enabled
                else None
            )
            readiness_alert_service = TradingReadinessAlertService(
                _build_readiness_observer(
                    application.state.usecases,
                    application.state.worker_versions,
                    None if settings.internal_worker_broker_id is None else str(settings.internal_worker_broker_id),
                ),
                alert_transport,
                environment=settings.application_environment,
                enabled=settings.telegram_alerts_enabled,
                poll_interval_seconds=settings.readiness_alert_poll_interval_seconds,
                startup_grace_seconds=settings.readiness_alert_startup_grace_seconds,
                heartbeat_confirmation_seconds=settings.readiness_alert_heartbeat_confirmation_seconds,
                control_confirmation_seconds=settings.readiness_alert_control_confirmation_seconds,
                outbox_confirmation_seconds=settings.readiness_alert_outbox_confirmation_seconds,
                retry_delays_seconds=settings.readiness_alert_retry_delays_seconds,
                queue_limit=settings.readiness_alert_queue_limit,
            )
            application.state.trading_readiness_alert_service = readiness_alert_service
            with bind_sync_runner(executor.run):
                if settings.daily_admission_preparation_enabled:
                    background_tasks.append(
                        asyncio.create_task(
                            application.state.daily_admission_preparation.run(),
                            name="daily-admission-preparation",
                        )
                    )
                if settings.telegram_alerts_enabled:
                    background_tasks.append(
                        asyncio.create_task(readiness_alert_service.run(), name="trading-readiness-alerts")
                    )
            with business_process():
                audit_event(LOGGER, "APPLICATION_STARTED", "Backend application started")
            yield
        finally:

            async def cleanup() -> None:
                try:
                    for task in background_tasks:
                        task.cancel()
                    if readiness_alert_service is not None:
                        await readiness_alert_service.close()
                    if background_tasks:
                        await asyncio.gather(*background_tasks, return_exceptions=True)
                finally:
                    try:
                        if executor is not None:
                            await executor.aclose()
                    finally:
                        try:
                            if analytics_http is not None:
                                await analytics_http.aclose()
                        finally:
                            try:
                                if market_gateway is not None:
                                    await market_gateway.close()
                            finally:
                                with business_process():
                                    audit_event(LOGGER, "APPLICATION_STOPPED", "Backend application stopped")
                                engine.dispose()

            cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup_task.result()
            if cancelled:
                raise asyncio.CancelledError

    application = FastAPI(
        title="MOEX Sentinel API",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = settings

    class LoginBody(BaseModel):
        username: str = Field(max_length=64)
        password: str = Field(max_length=1024)

    @application.middleware("http")
    async def protect_browser_api(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if test_auth_bypass or not path.startswith("/api/"):
            return await call_next(request)
        auth: OperatorAuth = request.app.state.operator_auth
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not auth.origin_allowed(request):
            return JSONResponse({"detail": "Invalid Origin"}, status_code=403, headers={"Cache-Control": "no-store"})
        if path not in {"/api/health", "/api/auth/login"}:
            cookie_name = auth.cookie_name(request)
            raw_id = request.cookies.get(cookie_name) if cookie_name else None
            session = auth.get_session(raw_id)
            if session is None:
                response: Response = JSONResponse({"detail": "Authentication required"}, status_code=401)
                response.headers["Cache-Control"] = "no-store"
                return response
            request.state.operator_session = session
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                supplied = request.headers.get("X-CSRF-Token", "")
                if not compare_digest(supplied.encode("utf-8"), session.csrf_token.encode("ascii")):
                    response = JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)
                    response.headers["Cache-Control"] = "no-store"
                    return response
        if (
            settings.broker_access_mode == "READ_ONLY"
            and request.method == "POST"
            and (
                (path.startswith("/api/instruments/") and path.endswith("/trade"))
                or (path.startswith("/api/trading-automations/") and path.rsplit("/", 1)[-1] in {"resume", "close"})
            )
        ):
            return JSONResponse({"detail": "READ_ONLY: trading commands are disabled"}, status_code=403)
        response = await call_next(request)
        if path != "/api/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.middleware("http")
    async def protect_internal_worker_api(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if settings.application_environment != "PROD" or not (
            path == "/internal/automaton/heartbeats"
            or path.startswith(
                (
                    "/internal/automaton/brokers/",
                    "/internal/automaton/runtime-instances",
                    "/internal/trading-admission/",
                )
            )
            or path
            in {
                "/internal/automation-commands",
                "/internal/automation-statuses",
                "/internal/automation-facts",
            }
        ):
            return await call_next(request)

        authenticator = request.app.state.worker_authenticator
        authorization = request.headers.get("Authorization")
        try:
            if path == "/internal/automaton/runtime-instances" and request.method == "POST":
                authenticator.verify_enrollment(authorization)
            else:
                request.state.worker_principal = await request.app.state.sync_executor.run(
                    partial(
                        authenticator.authenticate,
                        authorization=authorization,
                        runtime_instance_header=request.headers.get("X-Runtime-Instance-ID"),
                        runtime_secret=request.headers.get("X-Runtime-Secret"),
                    )
                )
        except WorkerAuthenticationError:
            return JSONResponse(
                {"detail": "Worker authentication required"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)

    @application.post("/api/auth/login")
    async def login(body: LoginBody, request: Request) -> Response:
        auth: OperatorAuth = request.app.state.operator_auth
        cookie_name = auth.cookie_name(request)
        if cookie_name is None:
            return JSONResponse({"detail": "Local login unavailable for this host"}, status_code=403)
        verified = await auth.verify(body.username, body.password)
        if verified is None:
            return JSONResponse({"detail": "Too many login attempts"}, status_code=429, headers={"Retry-After": "1"})
        if not verified:
            return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
        raw_id, session = auth.create_session()
        response = JSONResponse({"username": session.username, "csrf_token": session.csrf_token})
        response.set_cookie(
            cookie_name,
            raw_id,
            max_age=12 * 60 * 60,
            secure=not auth.insecure_loopback,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @application.get("/internal/runtime")
    @application.get("/api/runtime")
    async def runtime_configuration() -> dict[str, str]:
        return {"environment": settings.application_environment, "access_mode": settings.broker_access_mode}

    @application.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, str]:
        session = request.state.operator_session
        return {"username": session.username, "csrf_token": session.csrf_token}

    @application.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        auth: OperatorAuth = request.app.state.operator_auth
        cookie_name = auth.cookie_name(request)
        auth.revoke(request.cookies.get(cookie_name) if cookie_name else None)
        response = Response(status_code=204)
        response.delete_cookie(
            cookie_name or "", path="/", secure=not auth.insecure_loopback, httponly=True, samesite="strict"
        )
        return response

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
        with (
            business_process(process_id=inherited) as process_id,
            bind_sync_runner(request.app.state.sync_executor.run),
        ):
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
    application.include_router(diagnostics_router, prefix="/api")
    application.include_router(broker_router, prefix="/api")
    application.include_router(portfolio_router, prefix="/api")
    application.include_router(trading_summary_router, prefix="/api")
    application.include_router(trading_admission_router, prefix="/api")
    application.include_router(market_data_router, prefix="/api")
    application.include_router(instrument_router, prefix="/api")
    application.include_router(automation_router, prefix="/api")
    application.include_router(internal_automaton_router)
    application.include_router(internal_trading_facts_router)
    application.include_router(internal_trading_admission_router)
    application.include_router(internal_market_router)
    return application


app = create_app(configure_audit=True)
