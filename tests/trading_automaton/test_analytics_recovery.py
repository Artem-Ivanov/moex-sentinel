"""Broker outages must back off while recovery remains cancellable and observable."""

import asyncio
import logging

import httpx
import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from tests.trading_automaton.command_factory import command
from tests.trading_automaton.test_analytics_runtime import NOW, Source, frame, runtime


def test_repeated_analytics_transport_failures_log_transitions_only(caplog, monkeypatch):
    monkeypatch.setattr("trading_automaton.services.analytics_frame.LOGGER.disabled", False)

    async def scenario():
        source = Source(httpx.ReadTimeout("synthetic failure"))
        service, preparation, tick = runtime(source)
        await service.replace_commands((command(),))
        for _ in range(3):
            await service.run_once()
        assert len(preparation.calls) == 3
        assert not tick.calls
        source.value = frame()
        await service.run_once()
        await service.run_once()
        assert len(tick.calls) == 1
        await service.close()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    records = [r for r in caplog.records if r.name.endswith("analytics_frame")]
    assert [getattr(r, "reason_code", None) for r in records] == [
        "ANALYTICS_UNAVAILABLE",
        "ANALYTICS_TRANSPORT_RECOVERED",
    ]


def test_broker_preparation_retries_follow_odd_seconds_and_reset_after_success(monkeypatch):
    async def scenario():
        service, preparation, _ = runtime(Source(frame()), now=lambda: NOW)
        attempts = 0
        waits = []

        async def prepare(commands, snapshot):
            nonlocal attempts
            attempts += 1
            if attempts == 6:
                return
            if attempts == 8:
                service._closed.set()
                return
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Synthetic outage", retryable=True)

        async def wait_without_wall_time(awaitable, *, timeout):  # noqa: ASYNC109 - emulate asyncio.wait_for
            awaitable.close()
            waits.append(timeout)
            raise TimeoutError

        preparation.prepare = prepare
        monkeypatch.setattr(asyncio, "wait_for", wait_without_wall_time)
        await service.replace_commands((command(),))
        await service.run()
        assert waits[:5] == [1, 3, 5, 7, 9]
        assert waits[5] == 1  # Normal tick resumes after successful preparation.
        assert waits[6] == 1  # The next independent outage starts at the base delay.

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_limit", [0, 2, 5])
def test_exhausted_broker_retries_probe_until_preparation_recovers(monkeypatch, retry_limit):
    async def scenario():
        service, preparation, tick = runtime(Source(frame()))
        service._retry_limit = retry_limit
        calls = 0
        waits = []

        async def temporarily_unavailable(commands, snapshot):
            nonlocal calls
            calls += 1
            if calls <= retry_limit + 2:
                raise TInvestAdapterError("BROKER_UNAVAILABLE", "Synthetic outage", retryable=True)

        async def skip_delay(awaitable, *, timeout):  # noqa: ASYNC109 - emulate asyncio.wait_for
            awaitable.close()
            waits.append(timeout)
            if tick.calls:
                service._closed.set()
            await asyncio.sleep(0)
            raise TimeoutError

        preparation.prepare = temporarily_unavailable
        monkeypatch.setattr(asyncio, "wait_for", skip_delay)
        await service.replace_commands((command(),))
        task = asyncio.create_task(service.run())
        try:
            for _ in range(40):
                await asyncio.sleep(0)
            assert calls == retry_limit + 3
            assert waits == [2 * retry + 1 for retry in range(retry_limit)] + [60, 60, 1]
            assert len(tick.calls) == 1
            assert task.done()
        finally:
            await service.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_limit", [0, 2])
def test_close_interrupts_exhausted_broker_cooldown(monkeypatch, retry_limit):
    async def scenario():
        service, preparation, tick = runtime(Source(frame()))
        service._retry_limit = retry_limit
        calls = 0
        cooldown_entered = asyncio.Event()

        async def unavailable(commands, snapshot):
            nonlocal calls
            calls += 1
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Synthetic outage", retryable=True)

        async def wait_without_wall_time(awaitable, *, timeout):  # noqa: ASYNC109 - emulate asyncio.wait_for
            if timeout == 60:
                cooldown_entered.set()
                return await awaitable
            awaitable.close()
            await asyncio.sleep(0)
            raise TimeoutError

        preparation.prepare = unavailable
        monkeypatch.setattr(asyncio, "wait_for", wait_without_wall_time)
        await service.replace_commands((command(),))
        task = asyncio.create_task(service.run())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert cooldown_entered.is_set()
            assert not task.done()
            await service.close()
            for _ in range(5):
                await asyncio.sleep(0)
            assert task.done()
            assert calls == retry_limit + 1
            assert not tick.calls
        finally:
            await service.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_permanent_broker_error_waits_for_restart_without_repeating_calls(caplog, monkeypatch):
    monkeypatch.setattr("trading_automaton.runtime.analytics_broker.LOGGER.disabled", False)

    async def scenario():
        service, preparation, _ = runtime(Source(frame()))
        calls = 0

        async def denied(commands, snapshot):
            nonlocal calls
            calls += 1
            raise TInvestAdapterError("BROKER_UNAUTHORIZED", "Synthetic denial", retryable=False)

        preparation.prepare = denied
        await service.replace_commands((command(),))
        task = asyncio.create_task(service.run())
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()
            assert calls == 1
            await service.close()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any(getattr(r, "reason_code", None) == "BROKER_PREPARATION_BLOCKED" for r in caplog.records)
