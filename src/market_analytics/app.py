"""ASGI application for independent market analytics."""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from market_analytics.market_source import HttpMarketSource, MarketSourcePort
from market_analytics.service import AnalyticsService
from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest


def create_app(
    market_source: MarketSourcePort | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
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
            app.state.analytics = AnalyticsService(HttpMarketSource(client), now=now)
            yield

    app = FastAPI(title="Market Analytics", lifespan=lifespan)
    if market_source is not None:
        app.state.analytics = AnalyticsService(market_source, now=now)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "INVALID_ANALYTICS_REQUEST"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/v1/analytics/snapshots", response_model=AnalyticsSnapshot)
    async def snapshot(payload: AnalyticsSnapshotRequest, request: Request) -> AnalyticsSnapshot:
        try:
            return await request.app.state.analytics.snapshot(payload)
        except (httpx.HTTPError, ValueError, ArithmeticError):
            raise HTTPException(status_code=503, detail="MARKET_SOURCE_UNAVAILABLE") from None

    return app
