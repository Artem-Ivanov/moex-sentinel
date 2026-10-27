"""Dedicated Core process for periodic portfolio snapshots."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from moex_sentinel.composition import build_portfolio_snapshot_collector
from moex_sentinel.config import Settings
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from sentinel_contracts.audit import audit_event, business_process, configure_logging

LOGGER = logging.getLogger(__name__)


class CollectorPort(Protocol):
    async def execute(self) -> object: ...


WaitPort = Callable[[asyncio.Event, int], Awaitable[None]]


async def wait_for_stop(stop: asyncio.Event, seconds: int) -> None:
    """Wait for shutdown or the next collection interval."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run_forever(
    collector: CollectorPort,
    *,
    interval_seconds: int,
    stop: asyncio.Event,
    wait: WaitPort = wait_for_stop,
) -> None:
    """Collect immediately and then repeat until shutdown."""
    while not stop.is_set():
        try:
            await collector.execute()
        except Exception:
            LOGGER.exception("Portfolio snapshot collection failed")
        await wait(stop, interval_seconds)


async def run() -> None:
    """Build dependencies and serve the periodic collection loop."""
    settings = Settings()
    configure_logging("portfolio-snapshot-worker", level=settings.log_level, format=settings.log_format)
    engine = create_database_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
    )
    collector = build_portfolio_snapshot_collector(create_session_factory(engine), engine, settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    with business_process():
        audit_event(LOGGER, "APPLICATION_STARTED", "Portfolio snapshot worker started")
    try:
        await run_forever(
            collector,
            interval_seconds=settings.portfolio_snapshot_interval_seconds,
            stop=stop,
        )
    finally:
        engine.dispose()
        with business_process():
            audit_event(LOGGER, "APPLICATION_STOPPED", "Portfolio snapshot worker stopped")


def main() -> None:
    """Run the portfolio snapshot process."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
