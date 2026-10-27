"""App-owned capacity for actual unfinished synchronous jobs."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from typing import TypeVar

T = TypeVar("T")


class SyncExecutor:
    def __init__(self, *, capacity: int = 4) -> None:
        if capacity < 1:
            raise ValueError("Sync executor capacity must be positive.")
        self._capacity = capacity
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="core-db")
        self._jobs: dict[Future, asyncio.Future] = {}
        self._waiters: set[asyncio.Future] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None

    async def run(self, operation: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        while not self._closed and len(self._jobs) >= self._capacity:
            waiter = loop.create_future()
            self._waiters.add(waiter)
            try:
                await waiter
            finally:
                self._waiters.discard(waiter)
        if self._closed:
            raise RuntimeError("Sync executor is closed.")
        context = copy_context()
        job = self._executor.submit(context.run, operation)
        result = asyncio.wrap_future(job)
        self._jobs[job] = result
        # An abandoned HTTP waiter must neither cancel the job nor leak its error.
        result.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        job.add_done_callback(lambda done: loop.call_soon_threadsafe(self._completed, done))
        return await asyncio.shield(result)

    def _wake_waiters(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _completed(self, job: Future) -> None:
        self._jobs.pop(job, None)
        self._wake_waiters()

    async def _drain(self) -> None:
        await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        self._executor.shutdown(wait=True)

    async def aclose(self) -> None:
        self._closed = True
        self._wake_waiters()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError
