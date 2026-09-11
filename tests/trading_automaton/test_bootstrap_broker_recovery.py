"""Bootstrap broker failures must reach the runtime's bounded retry owner."""

import asyncio

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from tests.trading_automaton.services.test_broker_tick_preparation_service import (
    AdoptedBroker,
    Bootstrap,
    Hydration,
    Portfolio,
    PositionBootstrap,
    ReadyCommissions,
)
from tests.trading_automaton.services.test_streaming_runtime_coordinator_service import bootstrap_command
from tests.trading_automaton.test_analytics_runtime import FALLBACK, NOW, Source, Tick, frame
from trading_automaton.services.analytics_runtime import AnalyticsBrokerRuntime, AnalyticsMetricsCache
from trading_automaton.services.broker_tick_preparation import BrokerTickPreparationService


@pytest.mark.parametrize("failure_phase", ["portfolio", "commission"])
@pytest.mark.parametrize(
    ("retry_limit", "retryable", "expected_calls", "expected_delays"),
    [
        (0, True, 1, []),
        (2, True, 3, [1, 3]),
        (2, False, 1, []),
    ],
)
def test_bootstrap_uses_runtime_retry_budget_and_pauses_on_exhaustion_or_permanent_error(
    monkeypatch, failure_phase, retry_limit, retryable, expected_calls, expected_delays
):
    """Catch swallowed SDK errors that otherwise restart HOLD bootstrap on every ordinary tick."""

    async def scenario():
        attempts = []
        delays = []

        def fail():
            attempts.append(failure_phase)
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Synthetic bootstrap failure", retryable=retryable)

        class FailedBroker(AdoptedBroker):
            async def get_positions(self, account_id):
                if failure_phase == "portfolio":
                    fail()
                return await super().get_positions(account_id)

        class FailedCommissions(ReadyCommissions):
            async def refresh_if_due(self, request, provider, *, now):
                if failure_phase == "commission":
                    fail()
                return await super().refresh_if_due(request, provider, now=now)

        async def skip_delay(awaitable, *, timeout):  # noqa: ASYNC109 - emulate asyncio.wait_for
            awaitable.close()
            delays.append(timeout)
            await asyncio.sleep(0)
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", skip_delay)
        bootstrap = PositionBootstrap()
        hydration = Hydration()
        tick = Tick()
        value = bootstrap_command()
        preparation = BrokerTickPreparationService(
            FailedBroker(),
            Portfolio(),
            Bootstrap(),
            FailedCommissions(),
            hydration,
            position_bootstrap=bootstrap,
            now=lambda: NOW,
        )
        runtime = AnalyticsBrokerRuntime(
            Source(frame()),
            tick,
            preparation=preparation,
            metrics=AnalyticsMetricsCache(),
            source_id=str(value.broker_id),
            fallback=FALLBACK,
            now=lambda: NOW,
            retry_limit=retry_limit,
        )
        await runtime.replace_commands((value,))
        task = asyncio.create_task(runtime.run())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(attempts) == expected_calls
            assert delays == expected_delays
            assert not task.done()
            assert bootstrap.calls == []
            assert hydration.calls == []
            assert tick.calls == []
        finally:
            await runtime.close()
            await task

    asyncio.run(scenario())
