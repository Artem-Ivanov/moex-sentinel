import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sentinel_contracts.analytics import (
    AdaptiveThresholds,
    AnalyticsInstrument,
    AnalyticsSnapshot,
    MarketIndicators,
)
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook, StreamTradingStatus
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import BatchTickResult
from trading_automaton.domain.storage_dtos import BatchPersistResult
from trading_automaton.runtime.analytics_broker import AnalyticsBrokerRuntime
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.analytics_frame import AnalyticsFrameService, AnalyticsMetricsCache
from trading_automaton.services.decision import TradeDecision, TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.decision_materialization import DecisionMaterializerService
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.position_batch_scheduler import PositionBatchSchedulerService
from trading_automaton.services.streaming_batch_tick import StreamingBatchTickService
from trading_automaton.services.streaming_position_decision import (
    HydratedPositionState,
    StreamingPositionDecisionService,
)
from trading_automaton.storage.repository import IntentHistory, TradingCycleState
from trading_automaton.usecases.broker_iteration import RunBrokerIterationUsecase

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command(index: int) -> AutomationCommand:
    return baseline_command(automation=f"automation-{index}")


def hydrated(automation_id: str) -> HydratedPositionState:
    return HydratedPositionState(
        BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
        IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
        (),
        TradingCycleState(automation_id, Decimal("99"), None, True, None, NOW),
        MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
    )


class Analytics:
    async def snapshot(self, request):
        book = StreamOrderBook(
            "instrument", (OrderBookLevel(Decimal("100"), 1),), (OrderBookLevel(Decimal("100.1"), 1),), NOW, True
        )
        return AnalyticsSnapshot(
            snapshot_id="snapshot",
            profile_id=request.profile_id,
            captured_at=NOW,
            instruments=(
                AnalyticsInstrument(
                    instrument_id="instrument",
                    market=InstrumentMarketState(
                        "instrument",
                        order_book=book,
                        trading_status=StreamTradingStatus("instrument", "NORMAL_TRADING", True, True, NOW),
                    ),
                    candles=(),
                    available=True,
                    freshness="FRESH",
                    metrics=hydrated("unused").indicators,
                ),
            ),
        )

    async def close(self):
        pass


class States:
    def __init__(self) -> None:
        self.values = {
            str(command(index).automation_id): hydrated(str(command(index).automation_id)) for index in (1, 2)
        }

    async def get(self, automation_id: str):
        return self.values[automation_id]

    async def update(self, automation_id: str, value: HydratedPositionState) -> None:
        self.values[automation_id] = value


class Schedules:
    def schedule(self, key, *, snapshot_at):
        assert snapshot_at == NOW
        return CommissionSchedule(Decimal("0.001"), Decimal("0.001"))


class SpyStrategy:
    code = "SPY"
    version = "1"

    def __init__(self) -> None:
        self.calls_by_automation: dict[str, int] = {}

    def decide(self, context):
        automation_id = context.cycle.automation_id
        self.calls_by_automation[automation_id] = self.calls_by_automation.get(automation_id, 0) + 1
        return TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100.1"), "BUY")


class Batch:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.items = ()
        self.requests = ()
        self.called = asyncio.Event()

    async def run_batch(self, items, requests, *, snapshot_at):
        self.events.append("persist")
        self.items, self.requests = items, requests
        self.events.extend("sdk_dispatch" for _ in requests)
        self.called.set()
        return BatchTickResult(BatchPersistResult((), ()), ())


class Preparation:
    def __init__(self) -> None:
        self.calls = 0
        self.prepared = asyncio.Event()

    async def prepare(self, commands, snapshot) -> None:
        self.calls += 1
        self.prepared.set()


def test_one_analytics_frame_evaluates_each_position_once_before_sdk_dispatch() -> None:
    async def scenario():
        analytics = Analytics()
        states = States()
        strategy = SpyStrategy()
        batch = Batch()
        tick = StreamingBatchTickService(
            PositionBatchSchedulerService(
                StreamingPositionDecisionService(
                    Schedules(),
                    decisions=TradeDecisionService(strategy),
                    contexts=DecisionContextService(StrategySettings(enabled=True)),
                )
            ),
            states,
            batch,
            now=lambda: NOW,
            materializer=DecisionMaterializerService(settings=StrategySettings(enabled=True), id_factory=lambda: "id"),
            order_books=OrderBookValidationService(),
        )
        preparation = Preparation()
        runtime = AnalyticsBrokerRuntime(
            analytics,
            RunBrokerIterationUsecase(
                AnalyticsFrameService(analytics, now=lambda: NOW, order_books=OrderBookValidationService()),
                tick,
                preparation=preparation,
                metrics=AnalyticsMetricsCache(),
                source_id=str(command(1).broker_id),
                fallback=AdaptiveThresholds("0.5", "0.5", "STRATEGY"),
            ),
        )
        await runtime.replace_commands((command(1), command(2)))
        await runtime.run_once()
        await runtime.run_once()
        assert preparation.calls == 2
        await runtime.close()
        return strategy, batch

    strategy, batch = asyncio.run(scenario())

    assert strategy.calls_by_automation == {
        str(command(1).automation_id): 1,
        str(command(2).automation_id): 1,
    }
    assert batch.events.index("persist") < batch.events.index("sdk_dispatch")
    assert batch.items[0].estimated_commission == Decimal("1.0010")
    assert batch.requests[0].required_cash == Decimal("1002.0010")
