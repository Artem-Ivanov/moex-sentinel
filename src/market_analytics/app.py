"""ASGI application for independent market analytics."""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from market_analytics.indicators import MarketIndicatorsService
from market_analytics.market_source import HttpMarketSource
from market_analytics.ports import MarketSourcePort
from market_analytics.service import AnalyticsService
from market_analytics.usecases import AnalyticsUnavailableError, CalculateAnalyticsSnapshotUsecase
from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest


def create_app(
    market_source: MarketSourcePort | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Compose analytics dependencies and own the default HTTP client's lifespan."""
    calculator = AnalyticsService(MarketIndicatorsService(), now=now)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if market_source is not None:
            yield
            return
        async with httpx.AsyncClient(
            base_url=os.environ.get("ANALYTICS_CORE_URL", "http://backend:8000"),
            timeout=httpx.Timeout(2.0),
            trust_env=False,
        ) as client:
            app.state.analytics = CalculateAnalyticsSnapshotUsecase(HttpMarketSource(client), calculator)
            yield

    app = FastAPI(title="Market Analytics", lifespan=lifespan)
    if market_source is not None:
        app.state.analytics = CalculateAnalyticsSnapshotUsecase(market_source, calculator)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "INVALID_ANALYTICS_REQUEST"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/v1/analytics/snapshots", response_model=AnalyticsSnapshot)
    async def snapshot(payload: AnalyticsSnapshotRequest, request: Request) -> AnalyticsSnapshot:
        """Return the calculated snapshot or the stable public availability response."""
        try:
            return await request.app.state.analytics.execute(payload)
        except AnalyticsUnavailableError:
            raise HTTPException(status_code=503, detail="MARKET_SOURCE_UNAVAILABLE") from None

    return app
