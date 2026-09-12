import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from sentinel_contracts.analytics import (
    AdaptiveThresholds,
    AnalyticsInstrument,
    AnalyticsSnapshot,
    AnalyticsSnapshotRequest,
    MarketIndicators,
)
from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook, StreamTradingStatus
from sentinel_contracts.trading import DecisionKind
from tests.trading_automaton.command_factory import command
from trading_automaton.domain.dtos import PositionEvaluationResult, PositionWorkItem, TradeDecision
from trading_automaton.services.analytics_runtime import AnalyticsBrokerRuntime, AnalyticsMetricsCache
from trading_automaton.services.position_batch_scheduler import PositionBatchSchedulerService

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
FALLBACK = AdaptiveThresholds(Decimal("0.5"), Decimal("0.5"), "STRATEGY")


def frame(*, at=NOW, book_at=NOW, snapshot_id="snapshot", fresh="FRESH", ids=("instrument",)):
    request = AnalyticsSnapshotRequest(source_id=command().broker_id, instrument_ids=ids, fallback=FALLBACK)
    return AnalyticsSnapshot(
        snapshot_id=snapshot_id,
        captured_at=at,
        profile_id=request.profile_id,
        instruments=tuple(
            AnalyticsInstrument(
                instrument_id=instrument,
                market=InstrumentMarketState(
                    instrument_id=instrument,
                    order_book=StreamOrderBook(
                        instrument, (OrderBookLevel(Decimal(99), 5),), (OrderBookLevel(Decimal(100), 5),), book_at, True
                    ),
                    trading_status=StreamTradingStatus(instrument, "NORMAL_TRADING", True, True, NOW),
                ),
                candles=(),
                available=True,
                metrics=MarketIndicators(Decimal("0.5"), Decimal("0.5"), "STRATEGY", None, None, None, None),
                freshness=fresh,
            )
            for instrument in ids
        ),
    )


class Source:
    def __init__(self, value):
        self.value = value
        self.requests = []

    async def snapshot(self, request):
        self.requests.append(request)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    async def close(self):
        pass


class Preparation:
    def __init__(self, metrics, after=lambda: None):
        self.calls = []
        self.metrics = metrics
        self.after = after

    async def prepare(self, commands, snapshot):
        self.calls.append((commands, snapshot, await self.metrics.get("instrument")))
        self.after()


class Tick:
    def __init__(self):
        self.calls = []

    async def run_tick(self, commands, snapshot):
        self.calls.append((commands, snapshot))


def runtime(source, *, now=lambda: NOW, after=lambda: None):
    metrics = AnalyticsMetricsCache()
    preparation = Preparation(metrics, after)
    tick = Tick()
    result = AnalyticsBrokerRuntime(
        source,
        tick,
        preparation=preparation,
        metrics=metrics,
        source_id=str(command().broker_id),
        fallback=FALLBACK,
        now=now,
    )
    return result, preparation, tick


def test_one_batch_for_multiple_positions_shares_metrics_and_snapshot():
    async def scenario():
        source = Source(frame(ids=("instrument", "second")))
        service, preparation, tick = runtime(source)
        commands = (command(), command(automation="two"), command(automation="three", instrument="second"))
        await service.replace_commands(commands)
        await service.run_once()
        assert len(source.requests) == 1
        assert source.requests[0].instrument_ids == ("instrument", "second")
        assert tick.calls[0][0] == commands
        assert tick.calls[0][1].snapshot_id == "snapshot"
        assert preparation.calls[0][2] == source.value.instruments[0].metrics
        await service.run_once()
        assert len(tick.calls) == 1
        assert len(preparation.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(("age_ms", "expected"), [(2000, 1), (2001, 0), (-1, 0)])
def test_wall_clock_ttl_boundary(age_ms, expected):
    async def scenario():
        service, preparation, tick = runtime(Source(frame()), now=lambda: NOW + timedelta(milliseconds=age_ms))
        await service.replace_commands((command(),))
        await service.run_once()
        assert len(tick.calls) == expected
        assert len(preparation.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value",
    [
        frame(book_at=NOW - timedelta(seconds=3)),
        frame(fresh="UNAVAILABLE"),
        httpx.ReadTimeout("synthetic timeout"),
        ValueError("synthetic invalid response"),
    ],
)
def test_unavailable_analytics_preserves_preparation_and_recovers(value):
    async def scenario():
        source = Source(value)
        service, preparation, tick = runtime(source)
        await service.replace_commands((command(),))
        await service.run_once()
        assert tick.calls == []
        assert len(preparation.calls) == 1
        assert preparation.calls[0][1].instruments == {}
        assert preparation.calls[0][2] is None
        source.value = frame(snapshot_id="recovered")
        await service.run_once()
        assert len(tick.calls) == 1
        assert len(preparation.calls) == 2

    asyncio.run(scenario())


def test_preparation_consuming_ttl_cannot_start_decisions():
    async def scenario():
        clock = [NOW]
        service, _, tick = runtime(
            Source(frame()), now=lambda: clock[0], after=lambda: clock.__setitem__(0, NOW + timedelta(seconds=3))
        )
        await service.replace_commands((command(),))
        await service.run_once()
        assert tick.calls == []

    asyncio.run(scenario())


def test_mixed_batch_processes_only_fresh_instrument():
    async def scenario():
        value = frame(ids=("instrument", "second"))
        value = value.model_copy(
            update={
                "instruments": (value.instruments[0].model_copy(update={"freshness": "STALE"}), value.instruments[1])
            }
        )
        service, _, tick = runtime(Source(value))
        second = command(automation="second", instrument="second")
        await service.replace_commands((command(), second))
        await service.run_once()
        assert tick.calls[0][0] == (second,)

    asyncio.run(scenario())


@pytest.mark.parametrize("book_offset_ms", [-1000, 1, 101])
def test_runtime_and_scheduler_share_evaluation_time_without_extending_source_ttl(book_offset_ms):
    class Decider:
        async def decide(self, item, state):
            return PositionEvaluationResult(
                TradeDecision(DecisionKind.NO_ACTION, 0, None, "NO_THRESHOLD"), item.state, Decimal()
            )

    async def scenario():
        evaluation_at = NOW + timedelta(milliseconds=100)
        book_at = NOW + timedelta(milliseconds=book_offset_ms)
        service, _, tick = runtime(Source(frame(book_at=book_at)), now=lambda: evaluation_at)
        await service.replace_commands((command(),))
        await service.run_once()
        if book_at > evaluation_at:
            assert not tick.calls
            return
        commands, snapshot = tick.calls[0]
        result = await PositionBatchSchedulerService(Decider()).prepare(
            (PositionWorkItem(commands[0], False),), snapshot
        )
        assert result[0].decision.reason_code == "NO_THRESHOLD"
        assert snapshot.created_at == evaluation_at
        assert snapshot.expires_at == min(NOW, book_at) + timedelta(seconds=2)

    asyncio.run(scenario())
