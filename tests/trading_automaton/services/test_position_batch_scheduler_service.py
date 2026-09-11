import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    MarketBatchSnapshot,
    StreamOrderBook,
    StreamTradingStatus,
)
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.domain.dtos import HydratedPositionState, MarketIndicators
from trading_automaton.domain.storage_dtos import IntentHistory, TradingCycleState
from trading_automaton.services.broker_rate_limit import BrokerRateLimitService
from trading_automaton.services.decision import TradeDecision
from trading_automaton.services.position_batch_scheduler import (
    PositionBatchSchedulerService,
    PositionEvaluationResult,
    PositionWorkItem,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command(index: int) -> AutomationCommand:
    return baseline_command(automation=f"a{index}", broker="b1", instrument=f"i{index}")


def hydrated_state(index: int) -> HydratedPositionState:
    return HydratedPositionState(
        position=BrokerPosition(f"i{index}", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
        history=IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
        lots=(),
        cycle=TradingCycleState(f"a{index}", Decimal("99"), None, True, None, NOW),
        indicators=MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
    )


def market(
    instrument_id: str,
    captured_at: datetime = NOW,
    *,
    status_captured_at: datetime | None = None,
    include_status: bool = True,
    trading_available: bool = True,
    best_bid: Decimal = Decimal("100"),
    best_ask: Decimal = Decimal("100.1"),
) -> InstrumentMarketState:
    return InstrumentMarketState(
        instrument_id,
        order_book=StreamOrderBook(
            instrument_id,
            (OrderBookLevel(best_bid, 1),),
            (OrderBookLevel(best_ask, 1),),
            captured_at,
            True,
        ),
        trading_status=(
            StreamTradingStatus(
                instrument_id,
                "NORMAL" if trading_available else "CLOSED",
                trading_available,
                trading_available,
                status_captured_at or captured_at,
            )
            if include_status
            else None
        ),
    )


class ParallelDecider:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.entered = 0
        self.release = asyncio.Event()
        self.calls = []

    async def decide(self, item, state):
        self.calls.append(item)
        self.entered += 1
        if self.entered == self.expected:
            self.release.set()
        await self.release.wait()
        return PositionEvaluationResult(
            TradeDecision(DecisionKind.NO_ACTION, 0, None, "NO_THRESHOLD"),
            item.state,
            Decimal(),
        )


def test_all_positions_use_one_snapshot_and_calculate_in_parallel() -> None:
    async def scenario():
        decider = ParallelDecider(3)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable("snapshot-1", NOW, {f"i{i}": market(f"i{i}") for i in range(3)})
        result = await asyncio.wait_for(
            scheduler.prepare(tuple(PositionWorkItem(command(i), False) for i in range(3)), snapshot),
            timeout=1,
        )
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 3
    assert {item.snapshot_id for item in result} == {"snapshot-1"}


def test_scheduler_evaluates_each_eligible_position_once_in_parallel_with_prepared_state() -> None:
    async def scenario():
        decider = ParallelDecider(3)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable("snapshot-1", NOW, {f"i{i}": market(f"i{i}") for i in range(3)})
        states = tuple(hydrated_state(index) for index in range(3))
        items = tuple(PositionWorkItem(command(i), False, state=states[i], snapshot_at=NOW) for i in range(3))
        result = await asyncio.wait_for(scheduler.prepare(items, snapshot), timeout=1)
        return decider, result, states

    decider, result, states = asyncio.run(scenario())

    assert decider.entered == 3
    assert [call.state for call in decider.calls] == list(states)
    assert {item.snapshot_id for item in result} == {"snapshot-1"}
    assert [item.evaluation.state for item in result] == list(states)


def test_stale_order_book_and_active_intent_skip_decider() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {
                "i1": market("i1", NOW - timedelta(seconds=3)),
                "i2": market("i2"),
            },
        )
        return decider, await scheduler.prepare(
            (PositionWorkItem(command(1), False), PositionWorkItem(command(2), True)),
            snapshot,
        )

    decider, result = asyncio.run(scenario())

    assert decider.entered == 0
    assert [item.decision.reason_code for item in result] == [
        "STALE_ORDER_BOOK",
        "ACTIVE_INTENT",
    ]


def test_crossed_order_book_skips_strategy_decision() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {"i1": market("i1", best_bid=Decimal("100.1"), best_ask=Decimal("100"))},
        )
        result = await scheduler.prepare((PositionWorkItem(command(1), False),), snapshot)
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 0
    assert result[0].decision.reason_code == "CROSSED_ORDER_BOOK"


def test_old_status_with_fresh_order_book_reaches_decider() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {"i1": market("i1", status_captured_at=NOW - timedelta(hours=1))},
        )
        result = await scheduler.prepare((PositionWorkItem(command(1), False),), snapshot)
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 1
    assert result[0].decision.reason_code == "NO_THRESHOLD"


def test_missing_trading_status_has_specific_reason() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {"i1": market("i1", include_status=False)},
        )
        result = await scheduler.prepare((PositionWorkItem(command(1), False),), snapshot)
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 0
    assert result[0].decision.reason_code == "TRADING_STATUS_UNAVAILABLE"


def test_old_closed_trading_status_reports_closed_session() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable(
            "snapshot-1",
            NOW,
            {
                "i1": market(
                    "i1",
                    status_captured_at=NOW - timedelta(hours=1),
                    trading_available=False,
                )
            },
        )
        result = await scheduler.prepare((PositionWorkItem(command(1), False),), snapshot)
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 0
    assert result[0].decision.reason_code == "MARKET_SESSION_CLOSED"


def test_missing_commission_profile_skips_decider() -> None:
    async def scenario():
        decider = ParallelDecider(1)
        scheduler = PositionBatchSchedulerService(decider)
        snapshot = MarketBatchSnapshot.immutable("snapshot-1", NOW, {"i1": market("i1")})
        result = await scheduler.prepare(
            (PositionWorkItem(command(1), False, commission_profile_available=False),),
            snapshot,
        )
        return decider, result

    decider, result = asyncio.run(scenario())

    assert decider.entered == 0
    assert result[0].decision.reason_code == "COMMISSION_PROFILE_UNAVAILABLE"
    assert result[0].evaluation.estimated_commission == Decimal()


def test_rate_limit_wait_preserves_prepared_state_without_commission() -> None:
    class ExecutableDecider:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, item, state):
            self.calls += 1
            return PositionEvaluationResult(
                TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100"), "BUY"),
                item.state,
                Decimal("1.25"),
            )

    async def scenario():
        decider = ExecutableDecider()
        rate_limit = BrokerRateLimitService(capacity=1, refill_per_second=1)
        scheduler = PositionBatchSchedulerService(
            decider,
            rate_limit=rate_limit,
        )
        snapshot = MarketBatchSnapshot.immutable("snapshot-1", NOW, {"i1": market("i1")})
        prepared_state = hydrated_state(1)
        item = PositionWorkItem(command(1), False, state=prepared_state, snapshot_at=NOW)
        assert rate_limit.try_acquire(NOW) is True
        return await scheduler.prepare((item,), snapshot), decider, prepared_state

    result, decider, prepared_state = asyncio.run(scenario())

    assert result[0].decision.reason_code == "BROKER_RATE_LIMIT_BUDGET"
    assert result[0].evaluation.state is prepared_state
    assert result[0].evaluation.estimated_commission == Decimal()
    assert decider.calls == 1


@pytest.mark.parametrize("idle_kind", [DecisionKind.WAIT, DecisionKind.NO_ACTION])
def test_idle_positions_do_not_starve_later_orders_across_ticks(idle_kind: DecisionKind) -> None:
    admitted: set[str] = set()

    class Decider:
        async def decide(self, item, state):
            idle = str(item.command.automation_id) in admitted
            evaluated_state = item.state.model_copy(
                update={"cycle": item.state.cycle.model_copy(update={"pending_low": Decimal("98")})}
            )
            return PositionEvaluationResult(
                TradeDecision(
                    idle_kind if idle else DecisionKind.BUY_MORE,
                    0 if idle else 1,
                    None if idle else Decimal("100"),
                    "NO_THRESHOLD" if idle else "BUY",
                ),
                evaluated_state,
                Decimal(),
            )

    async def scenario():
        scheduler = PositionBatchSchedulerService(
            Decider(), rate_limit=BrokerRateLimitService(capacity=2, refill_per_second=2)
        )
        items = tuple(PositionWorkItem(command(index), False, state=hydrated_state(index)) for index in range(6))
        for tick in range(3):
            at = NOW + timedelta(seconds=tick)
            snapshot = MarketBatchSnapshot.immutable(
                f"snapshot-{tick}", at, {f"i{i}": market(f"i{i}", at) for i in range(6)}
            )
            prepared = await scheduler.prepare(items, snapshot)
            executable = [result for result in prepared if result.decision.kind is DecisionKind.BUY_MORE]
            assert len(executable) == 2
            for result in prepared:
                if result.decision.reason_code == "NO_THRESHOLD":
                    assert result.evaluation.state.cycle.pending_low == Decimal("98")
            admitted.update(str(result.command.automation_id) for result in executable)
        assert len(admitted) == 6

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [DecisionKind.BUY_MORE, DecisionKind.SELL_PART, DecisionKind.SELL_ALL])
def test_order_decisions_remain_limited_by_shared_dispatch_capacity(kind: DecisionKind) -> None:
    class Decider:
        async def decide(self, item, state):
            return PositionEvaluationResult(TradeDecision(kind, 1, Decimal("100"), "ORDER"), item.state, Decimal())

    async def scenario():
        scheduler = PositionBatchSchedulerService(
            Decider(), rate_limit=BrokerRateLimitService(capacity=2, refill_per_second=2)
        )
        snapshot = MarketBatchSnapshot.immutable("snapshot", NOW, {f"i{i}": market(f"i{i}") for i in range(6)})
        return await scheduler.prepare(tuple(PositionWorkItem(command(i), False) for i in range(6)), snapshot)

    results = asyncio.run(scenario())
    assert sum(result.decision.kind is kind for result in results) == 2
    assert sum(result.decision.reason_code == "BROKER_RATE_LIMIT_BUDGET" for result in results) == 4
