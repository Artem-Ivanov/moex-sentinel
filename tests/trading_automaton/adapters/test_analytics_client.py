"""HTTP failures are translated at the adapter; cancellation is preserved."""

import asyncio

import httpx
import pytest

from sentinel_contracts.analytics import AdaptiveThresholds, AnalyticsSnapshotRequest
from tests.trading_automaton.test_analytics_runtime import command
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.domain.errors import AnalyticsUnavailableError


@pytest.mark.parametrize("failure", ["http", "transport", "cancel"])
def test_snapshot_failure_contract(failure):
    async def scenario():
        def handler(request):
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "transport":
                raise httpx.ReadTimeout("SECRET", request=request)
            return httpx.Response(503, text="SECRET")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://analytics.test") as http:
            adapter = AnalyticsClient(http)
            request = AnalyticsSnapshotRequest(
                source_id=command().broker_id,
                instrument_ids=("instrument",),
                fallback=AdaptiveThresholds("0.5", "0.5", "TEST"),
            )
            error = asyncio.CancelledError if failure == "cancel" else AnalyticsUnavailableError
            with pytest.raises(error) as caught:
                await adapter.snapshot(request)
            assert "SECRET" not in str(caught.value)

    asyncio.run(scenario())
