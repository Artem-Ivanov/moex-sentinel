"""Timing policy for the single owner of a Core market source."""

import asyncio
from collections.abc import Awaitable, Callable
from math import isfinite
from typing import Any


class MarketRecoveryPolicy:
    def __init__(
        self,
        *,
        retry_seconds: float = 1.0,
        retry_limit: int = 5,
        operation_timeout_seconds: float = 10.0,
        close_timeout_seconds: float = 5.0,
        quiet_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        values = (retry_seconds, operation_timeout_seconds, close_timeout_seconds, quiet_seconds)
        if any(not isfinite(value) or value <= 0 for value in values):
            raise ValueError("Recovery intervals must be finite and positive.")
        if type(retry_limit) is not int or retry_limit < 0:
            raise ValueError("Retry limit must be a nonnegative integer.")
        self.retry_seconds = retry_seconds
        self.retry_limit = retry_limit
        self.operation_timeout_seconds = operation_timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self.quiet_seconds = quiet_seconds
        self.sleep = sleep

    def delay(self, failures: int) -> float:
        return self.retry_seconds * (2 * failures - 1)


class MarketSourceOperations:
    """Retain ownership of cancelled calls until they actually finish."""

    def __init__(self) -> None:
        self.pending: set[asyncio.Future[Any]] = set()

    async def run[T](self, operation: Awaitable[T], deadline_seconds: float) -> T:
        task = asyncio.ensure_future(operation)
        self.pending.add(task)
        task.add_done_callback(self._finished)
        try:
            done, _ = await asyncio.wait((task,), timeout=deadline_seconds)
            if not done:
                task.cancel()
                raise TimeoutError("Market operation deadline exceeded")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _finished(self, task: asyncio.Future[Any]) -> None:
        self.pending.discard(task)
        if not task.cancelled():
            task.exception()

    async def drain(self) -> None:
        if self.pending:
            await asyncio.wait(tuple(self.pending))
