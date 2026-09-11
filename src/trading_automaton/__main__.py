"""Trading automaton process entry point."""

import asyncio
import logging
import signal
from contextlib import suppress

from sentinel_contracts.audit import audit_event, business_process, configure_logging
from trading_automaton.composition import build_streaming_runtime
from trading_automaton.config import AutomatonSettings, StrategySettings
from trading_automaton.services.recovery import RecoveryService

LOGGER = logging.getLogger(__name__)


async def run() -> None:
    settings = AutomatonSettings()  # type: ignore[call-arg]
    strategy_settings = StrategySettings()
    configure_logging("trading-automaton", level=settings.log_level, format=settings.log_format)
    runtime, repository, http = build_streaming_runtime(settings, strategy_settings)
    unclean_shutdown = repository.begin_run(settings.worker_id)
    RecoveryService(repository).recover(unclean_shutdown=unclean_shutdown)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_requested.set)
    with business_process():
        audit_event(LOGGER, "APPLICATION_STARTED", "Trading automaton started")
    try:
        while not stop_requested.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await runtime.run_iteration()
            except Exception:
                LOGGER.exception("Trading automaton iteration failed")
            elapsed = asyncio.get_running_loop().time() - started
            timeout = max(0.0, settings.iteration_seconds - elapsed)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_requested.wait(), timeout=timeout)
    finally:
        with business_process():
            audit_event(LOGGER, "APPLICATION_STOPPED", "Trading automaton stopped")
        await runtime.close()
        repository.finish_run(settings.worker_id)
        http.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        with business_process():
            audit_event(LOGGER, "APPLICATION_STOPPED", "Trading automaton interrupted")


if __name__ == "__main__":
    main()
