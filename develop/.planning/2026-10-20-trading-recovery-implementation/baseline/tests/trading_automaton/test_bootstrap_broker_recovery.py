"""Portfolio adoption and trading commission failures share the runtime retry owner."""

import asyncio

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.trading import AutomationState
from tests.trading_automaton.analytics_runtime_helpers import build_analytics_runtime
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
from trading_automaton.services.analytics_frame import AnalyticsMetricsCache
from trading_automaton.services.broker_tick_preparation import BrokerTickPreparationService


@pytest.mark.parametrize("failure_phase", ["portfolio", "commission"])
@pytest.mark.parametrize(
    ("retry_limit", "retryable", "expected_calls", "expected_delays"),
    [
        (0, True, 1, [60]),
        (2, True, 3, [1, 3, 60]),
        (2, False, 1, []),
    ],
)
def test_preparation_uses_runtime_retry_budget_then_cooldown_or_permanent_pause(
    monkeypatch, failure_phase, retry_limit, retryable, expected_calls, expected_delays
):
    """Adoption skips commission lookup; later trading preparation must still bound SDK retries."""

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
            delays.append(timeout)
            if timeout == 60:
                return await awaitable
            awaitable.close()
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
        runtime = build_analytics_runtime(
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
        if failure_phase == "commission":
            # Adoption must succeed without consulting the failing commission provider.
            await runtime.run_once()
            assert bootstrap.calls == [value]
            assert attempts == []
            assert tick.calls == []
            # Core acknowledgement subsequently authorizes regular preparation.
            await runtime.replace_commands((value.model_copy(update={"state": AutomationState.IN_WORK}),))
        task = asyncio.create_task(runtime.run())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(attempts) == expected_calls
            assert delays == expected_delays
            assert not task.done()
            assert bootstrap.calls == ([value] if failure_phase == "commission" else [])
            assert hydration.calls == []
            assert tick.calls == []
        finally:
            await runtime.close()
            await task

    asyncio.run(scenario())
