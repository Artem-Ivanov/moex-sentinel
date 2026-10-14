import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel_contracts.analytics import MarketIndicators
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision import TradeDecision, TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.order_quota import apply_order_quota
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


class PositionCount:
    async def positive_security_count(self, account_id):
        assert account_id == "account"
        return 1


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


def market(price: Decimal = Decimal("100")) -> InstrumentMarketState:
    return InstrumentMarketState(
        "instrument",
        order_book=StreamOrderBook("instrument", (OrderBookLevel(price, 1),), (OrderBookLevel(price, 1),), NOW, True),
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


def test_quota_caps_buy_using_free_cash_percent_and_exchange_lot_size() -> None:
    class AvailableCash:
        async def available(self, account_id, currency):
            assert (account_id, currency) == ("account", "RUB")
            return Decimal("5000")

        async def reserved(self, account_id, currency):
            return Decimal()

    decisions = Decisions(TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("50"), "BUY"))
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("10"))
    item = PositionWorkItem(value, False, state=state(), snapshot_at=NOW)
    service = StreamingPositionDecisionService(
        Commissions(),
        cash=AvailableCash(),
        positions=PositionCount(),
        decisions=decisions,
        contexts=DecisionContextService(StrategySettings()),
    )

    result = asyncio.run(service.decide(item, market(Decimal("50"))))

    assert (result.decision.kind, result.decision.quantity_lots) == (DecisionKind.BUY_MORE, 10)
    assert result.estimated_commission == Decimal("0.3125")


def test_real_streaming_strategy_buys_ten_lots_from_approved_example() -> None:
    class AvailableCash:
        async def available(self, account_id, currency):
            return Decimal("5000")

        async def reserved(self, account_id, currency):
            return Decimal()

    indicators = MarketIndicators(
        Decimal("0.5"),
        Decimal("0.5"),
        "TEST",
        Decimal("50"),
        Decimal("50"),
        Decimal(),
        NOW,
        Decimal("50"),
        Decimal("50"),
        Decimal("45"),
        Decimal("55"),
    )
    hydrated = HydratedPositionState(
        BrokerPosition("instrument", Decimal(), Decimal(), Decimal("50"), "RUB"),
        IntentHistory(Decimal("50"), 0, Decimal(), 0, Decimal()),
        (),
        TradingCycleState("automation", Decimal("49"), None, True, None, NOW),
        indicators,
    )
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("10"))
    item = PositionWorkItem(value, False, state=hydrated, snapshot_at=NOW)
    service = StreamingPositionDecisionService(
        Commissions(),
        cash=AvailableCash(),
        positions=PositionCount(),
        decisions=TradeDecisionService(),
        contexts=DecisionContextService(StrategySettings(enabled=True)),
    )

    result = asyncio.run(service.decide(item, market(Decimal("50"))))

    assert (result.decision.kind, result.decision.quantity_lots) == (DecisionKind.BUY_MORE, 10)


def test_quota_quantity_and_required_cash_replace_legacy_buy_order_lots() -> None:
    class AvailableCash:
        async def available(self, account_id, currency):
            return Decimal("5000")

        async def reserved(self, account_id, currency):
            return Decimal()

    captured = []
    real_decisions = TradeDecisionService()

    class CapturingDecision:
        def decide(self, context):
            captured.append(context)
            return real_decisions.decide(context)

    indicators = MarketIndicators(
        Decimal("0.5"),
        Decimal("0.5"),
        "TEST",
        Decimal("50"),
        Decimal("50"),
        Decimal(),
        NOW,
        Decimal("50"),
        Decimal("50"),
        Decimal("45"),
        Decimal("55"),
    )
    hydrated = HydratedPositionState(
        BrokerPosition("instrument", Decimal(), Decimal(), Decimal("50"), "RUB"),
        IntentHistory(Decimal("50"), 0, Decimal(), 0, Decimal()),
        (),
        TradingCycleState("automation", Decimal("49"), None, True, None, NOW),
        indicators,
    )
    value = baseline_command(lot_size=10, order_quota_percent=Decimal("10"))
    service = StreamingPositionDecisionService(
        Commissions(),
        cash=AvailableCash(),
        positions=PositionCount(),
        decisions=CapturingDecision(),
        contexts=DecisionContextService(StrategySettings(enabled=True, buy_order_lots=20)),
    )

    result = asyncio.run(
        service.decide(PositionWorkItem(value, False, state=hydrated, snapshot_at=NOW), market(Decimal("50")))
    )

    assert (result.decision.kind, result.decision.quantity_lots) == (DecisionKind.BUY_MORE, 1)
    assert captured[0].required_order_cash == Decimal("500.3125")


def test_quota_rounding_to_zero_waits_without_forcing_one_lot() -> None:
    class SmallCash:
        async def available(self, account_id, currency):
            return Decimal("499")

        async def reserved(self, account_id, currency):
            return Decimal()

    decisions = Decisions(TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("50"), "BUY"))
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("10"))
    service = StreamingPositionDecisionService(
        Commissions(),
        cash=SmallCash(),
        positions=PositionCount(),
        decisions=decisions,
        contexts=DecisionContextService(StrategySettings()),
    )

    result = asyncio.run(
        service.decide(PositionWorkItem(value, False, state=state(), snapshot_at=NOW), market(Decimal("50")))
    )

    assert (result.decision.kind, result.decision.quantity_lots) == (DecisionKind.WAIT, 0)


def test_quota_uses_cash_remaining_after_shared_reservations() -> None:
    class ReservedCash:
        async def available(self, account_id, currency):
            return Decimal("4750")

        async def reserved(self, account_id, currency):
            return Decimal("250")

    decisions = Decisions(TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("50"), "BUY"))
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("10"))
    service = StreamingPositionDecisionService(
        Commissions(),
        cash=ReservedCash(),
        positions=PositionCount(),
        decisions=decisions,
        contexts=DecisionContextService(StrategySettings()),
    )

    result = asyncio.run(
        service.decide(PositionWorkItem(value, False, state=state(), snapshot_at=NOW), market(Decimal("50")))
    )

    assert (result.decision.kind, result.decision.quantity_lots) == (DecisionKind.BUY_MORE, 9)


def test_quota_caps_partial_sell_but_does_not_cap_full_close() -> None:
    partial = Decisions(TradeDecision(DecisionKind.SELL_PART, 6, Decimal("50"), "SELL"))
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("10"))
    common = {
        "cash": type(
            "Cash",
            (),
            {
                "available": lambda self, _account, _currency: asyncio.sleep(0, result=Decimal("1000")),
                "reserved": lambda self, _account, _currency: asyncio.sleep(0, result=Decimal()),
            },
        )(),
        "positions": PositionCount(),
    }
    service = StreamingPositionDecisionService(
        Commissions(), decisions=partial, contexts=DecisionContextService(StrategySettings()), **common
    )
    capped = asyncio.run(
        service.decide(PositionWorkItem(value, False, state=state(), snapshot_at=NOW), market(Decimal("50")))
    )
    assert (capped.decision.kind, capped.decision.quantity_lots) == (DecisionKind.SELL_PART, 2)

    full = Decisions(TradeDecision(DecisionKind.SELL_ALL, 6, Decimal("50"), "STOP_LOSS"))
    service = StreamingPositionDecisionService(
        Commissions(), decisions=full, contexts=DecisionContextService(StrategySettings()), **common
    )
    unchanged = asyncio.run(
        service.decide(PositionWorkItem(value, False, state=state(), snapshot_at=NOW), market(Decimal("50")))
    )
    assert (unchanged.decision.kind, unchanged.decision.quantity_lots) == (DecisionKind.SELL_ALL, 6)


@pytest.mark.parametrize("kind", [DecisionKind.BUY_MORE, DecisionKind.SELL_PART])
def test_quota_floor_is_exact_beyond_decimal_context_precision(kind) -> None:
    value = baseline_command(lot_size=1, order_quota_percent=Decimal("0.000000003"))
    decision = TradeDecision(kind, 500_000_000, Decimal("1"), "TEST")

    capped = apply_order_quota(
        decision,
        value,
        available_free_cash=Decimal("6999999999999999999.999999999"),
        positive_position_count=1,
        best_bid=Decimal("1"),
        best_ask=Decimal("1"),
    )

    assert capped.quantity_lots == 209_999_999


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
