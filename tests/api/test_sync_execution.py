"""Executor capacity belongs to unfinished work, including abandoned waiters."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from moex_sentinel.api import sync_execution as execution_module
from moex_sentinel.api.sync_execution import SyncExecutor


async def wait_for_event(event):
    async with asyncio.timeout(3):
        while not event.is_set():  # noqa: ASYNC110 - cross-thread test barrier
            await asyncio.sleep(0.001)


def test_raw_cancellation_keeps_capacity_until_job_completion_and_copies_context():
    marker = ContextVar("executor_test_marker", default="outside")

    async def scenario():
        executor = SyncExecutor(capacity=1)
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        observed = []

        def blocked():
            observed.append(marker.get())
            entered.set()
            assert release.wait(3)
            return 1

        marker.set("first")
        first = asyncio.create_task(executor.run(blocked))
        await wait_for_event(entered)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        marker.set("second")
        second = asyncio.create_task(executor.run(lambda: (second_started.set(), marker.get())[1]))
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
        release.set()
        assert await second == "second"
        assert observed == ["first"]
        await executor.aclose()

    asyncio.run(scenario())


def test_queued_cancellation_exception_cleanup_and_independent_pools():
    async def scenario():
        first, other = SyncExecutor(capacity=1), SyncExecutor(capacity=1)
        entered, release = threading.Event(), threading.Event()
        ran = []

        def blocked():
            entered.set()
            assert release.wait(3)

        job = asyncio.create_task(first.run(blocked))
        await wait_for_event(entered)
        queued = asyncio.create_task(first.run(lambda: ran.append("cancelled")))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert await other.run(lambda: "independent") == "independent"
        release.set()
        await job

        def failed():
            raise ValueError("synthetic worker failure")

        with pytest.raises(ValueError, match="synthetic"):
            await first.run(failed)
        assert await first.run(lambda: 42) == 42
        assert ran == []
        await first.aclose()
        await other.aclose()

    asyncio.run(scenario())


def test_repeated_close_cancellation_still_drains_and_rejects_queued_and_late_jobs():
    async def scenario():
        executor = SyncExecutor(capacity=1)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked():
            entered.set()
            assert release.wait(3)
            finished.set()

        job = asyncio.create_task(executor.run(blocked))
        await wait_for_event(entered)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        queued = asyncio.create_task(executor.run(lambda: "must not run"))
        await asyncio.sleep(0)
        close = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        with pytest.raises(RuntimeError, match="closed"):
            await queued
        assert not close.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert finished.is_set()
        with pytest.raises(RuntimeError, match="closed"):
            await executor.run(lambda: "late")
        await executor.aclose()

    asyncio.run(scenario())


def test_capacity_two_bounds_submitted_unfinished_futures_after_waiter_cancellation(monkeypatch):

    submitted = []

    class ObservedExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            submitted.append(future)
            assert sum(not job.done() for job in submitted) <= 2
            return future

    monkeypatch.setattr(execution_module, "ThreadPoolExecutor", ObservedExecutor)

    async def scenario():
        executor = SyncExecutor(capacity=2)
        first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()

        def blocked(entered):
            entered.set()
            assert release.wait(3)

        first = asyncio.create_task(executor.run(lambda: blocked(first_entered)))
        second = asyncio.create_task(executor.run(lambda: blocked(second_entered)))
        try:
            await wait_for_event(first_entered)
            await wait_for_event(second_entered)
            first.cancel()
            second.cancel()
            for task in (first, second):
                with pytest.raises(asyncio.CancelledError):
                    await task
            queued = asyncio.create_task(executor.run(lambda: 3))
            await asyncio.sleep(0.02)
            assert len(submitted) == 2
            release.set()
            assert await queued == 3
        finally:
            release.set()
            await executor.aclose()

    asyncio.run(scenario())


def test_abandoned_worker_exception_is_consumed_and_capacity_recovers():
    async def scenario():
        executor = SyncExecutor(capacity=1)
        entered, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_handler, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        def failure():
            entered.set()
            assert release.wait(3)
            raise ValueError("synthetic abandoned failure")

        try:
            task = asyncio.create_task(executor.run(failure))
            await wait_for_event(entered)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await executor.run(lambda: 42) == 42
            await executor.aclose()
            await asyncio.sleep(0)
            assert errors == []
        finally:
            release.set()
            await executor.aclose()
            loop.set_exception_handler(original_handler)

    asyncio.run(scenario())
