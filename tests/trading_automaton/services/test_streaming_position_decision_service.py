import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision import TradeDecision
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.services.position_batch_scheduler import PositionWorkItem
from trading_automaton.services.streaming_position_decision import (
    HydratedPositionState,
    StreamingPositionDecisionService,
)
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command() -> AutomationCommand:
    return baseline_command()


class Decisions:
    def __init__(self, decision: TradeDecision | None = None) -> None:
        self.contexts = []
        self._decision = decision or TradeDecision(DecisionKind.BUY_MORE, 2, Decimal("100"), "BUY")

    def decide(self, context):
        self.contexts.append(context)
        return self._decision


class Commissions:
    def __init__(self, schedule: CommissionSchedule | None = None) -> None:
        self.schedule_value = schedule or CommissionSchedule(Decimal("0.000625"), Decimal("0.001"))
        self.calls = []

    def schedule(self, key, *, snapshot_at):
        self.calls.append((key, snapshot_at))
        return self.schedule_value


class Cash:
    async def available(self, account_id, currency):
        assert (account_id, currency) == ("account", "RUB")
        return Decimal("750")

    async def reserved(self, account_id, currency):
        assert (account_id, currency) == ("account", "RUB")
        return Decimal("250")


def state() -> HydratedPositionState:
    indicators = MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW)
    return HydratedPositionState(
        BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
        IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
        (),
        TradingCycleState("automation", Decimal("99"), None, True, None, NOW),
        indicators,
    )


def work_item(value: HydratedPositionState | None = None, *, snapshot_at: datetime = NOW) -> PositionWorkItem:
    return PositionWorkItem(command(), False, state=value or state(), snapshot_at=snapshot_at)


def market() -> InstrumentMarketState:
    return InstrumentMarketState(
        "instrument",
        order_book=StreamOrderBook(
            "instrument", (OrderBookLevel(Decimal("100"), 1),), (OrderBookLevel(Decimal("100.1"), 1),), NOW, True
        ),
    )


def test_evaluates_executable_position_once_and_materializes_exact_buy_commission() -> None:
    decisions = Decisions()
    commissions = Commissions()
    prepared_state = state()
    service = StreamingPositionDecisionService(
        commissions, decisions=decisions, contexts=DecisionContextService(StrategySettings())
    )

    # Replaces the historical zero-commission re-evaluation defect.
    result = asyncio.run(service.decide(work_item(prepared_state), market()))

    assert len(decisions.contexts) == 1
    assert decisions.contexts[0].cycle is prepared_state.cycle
    assert decisions.contexts[0].indicators is prepared_state.indicators
    assert decisions.contexts[0].commission_schedule == commissions.schedule_value
    assert commissions.calls[0][1] == NOW
    assert result.decision.kind is DecisionKind.BUY_MORE
    assert result.state is prepared_state
    assert result.estimated_commission == Decimal("1.25")


def test_materializes_exact_sell_commission_from_its_own_decision_amount() -> None:
    decisions = Decisions(TradeDecision(DecisionKind.SELL_PART, 2, Decimal("100"), "SELL"))
    service = StreamingPositionDecisionService(
        Commissions(), decisions=decisions, contexts=DecisionContextService(StrategySettings())
    )

    result = asyncio.run(service.decide(work_item(), market()))

    assert result.estimated_commission == Decimal("2.000")


def test_builds_decision_with_current_account_cash_and_reservations_once() -> None:
    class CapturingDecisions(Decisions):
        def __init__(self):
            super().__init__()
            self.cash = []

        def decide(self, context):
            self.cash.append((context.free_cash, context.reserved_cash, context.available_free_cash))
            return super().decide(context)

    decisions = CapturingDecisions()
    service = StreamingPositionDecisionService(
        Commissions(), cash=Cash(), decisions=decisions, contexts=DecisionContextService(StrategySettings())
    )

    asyncio.run(service.decide(work_item(), market()))

    assert decisions.cash == [(Decimal("1000"), Decimal("250"), Decimal("750"))]


def test_missing_hydrated_state_waits_without_strategy_call() -> None:
    decisions = Decisions()
    service = StreamingPositionDecisionService(
        Commissions(), decisions=decisions, contexts=DecisionContextService(StrategySettings())
    )

    result = asyncio.run(service.decide(PositionWorkItem(command(), False, state=None, snapshot_at=NOW), market()))

    assert result.decision.reason_code == "POSITION_STATE_UNAVAILABLE"
    assert decisions.contexts == []


def test_missing_or_expired_schedule_waits_without_strategy_call() -> None:
    decisions = Decisions()
    commissions = Commissions(schedule=None)
    commissions.schedule_value = None
    service = StreamingPositionDecisionService(
        commissions, decisions=decisions, contexts=DecisionContextService(StrategySettings())
    )

    result = asyncio.run(service.decide(work_item(snapshot_at=NOW + timedelta(days=1)), market()))

    assert result.decision.reason_code == "COMMISSION_PROFILE_UNAVAILABLE"
    assert decisions.contexts == []
