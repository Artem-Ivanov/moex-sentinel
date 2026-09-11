"""Unit tests for decision cycle evaluator boundaries in trading runtime."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel, OrderBookSnapshot
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import DecisionContext, MarketIndicators, TradeDecision
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.position_decision_evaluator import PositionDecisionEvaluator
from trading_automaton.services.runtime_decision_planner import DecisionPlan
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 11, 10, tzinfo=UTC)


def _command() -> AutomationCommand:
    return baseline_command(
        automation="automation-eval",
        broker="broker-1",
        account="account-1",
        instrument="instrument-1",
    )


def _indicators() -> MarketIndicators:
    return MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="ADAPTIVE",
        mean_5=Decimal("100"),
        mean_20=Decimal("100"),
        change_10_percent=Decimal("0.2"),
        last_candle_at=NOW,
        last_candle_open=Decimal("100"),
        last_candle_close=Decimal("101"),
        range_low=Decimal("95"),
        range_high=Decimal("105"),
    )


def _history() -> IntentHistory:
    return IntentHistory(
        last_buy_price=Decimal("100"),
        completed_partial_sell_steps=0,
        actual_commissions=Decimal(),
        total_bought_lots=0,
        buy_commissions=Decimal(),
    )


class _SpyPlanner:
    def __init__(self) -> None:
        self.last_cycle = None

    async def plan(
        self,
        *,
        command,
        position,
        order_book,
        history,
        lots,
        cycle,
        indicators,
        session_open: bool,
        estimator,
    ):
        self.last_cycle = cycle
        context = DecisionContext(
            core_available=True,
            has_active_intent=False,
            quantity_lots=int(position.quantity_lots),
            lot_size=command.lot_size,
            average_price=position.average_price,
            current_price=order_book.best_bid.price,
            best_bid=order_book.best_bid.price,
            best_ask=order_book.best_ask.price,
            last_buy_price=history.last_buy_price,
            invested_amount=Decimal("0"),
            commission_schedule=_commission_schedule(),
            buy_commission_per_lot=Decimal("0"),
            averaging_step_percent=indicators.averaging_step_percent,
            minimum_net_profit_percent=indicators.minimum_net_profit_percent,
            completed_partial_sell_steps=history.completed_partial_sell_steps,
            settings=StrategySettings(),
            currency=command.currency,
            min_price_increment=Decimal("0.01"),
            lots=lots,
            cycle=cycle,
            indicators=indicators,
            free_cash=Decimal("1000"),
            reserved_cash=Decimal(),
        )
        return DecisionPlan(
            indicators=indicators,
            context=context,
            decision=TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="SPY_WAIT",
            ),
        )


def _commission_schedule() -> CommissionSchedule:
    return CommissionSchedule(Decimal("0"), Decimal("0"))


def _run(async_task: object) -> object:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(async_task)
    finally:
        loop.close()


def test_evaluator_updates_cycle_on_flat_entry() -> None:
    planner = _SpyPlanner()
    evaluator = PositionDecisionEvaluator(
        now=lambda: NOW,
        planner=planner,
    )
    cycle = TradingCycleState(
        automation_id="automation-eval",
        pending_low=None,
        last_buy_candle_at=None,
        sell_armed=False,
        last_sell_price=Decimal("102"),
        updated_at=NOW,
    )
    position = BrokerPosition("instrument-1", Decimal("0"), Decimal("0"), Decimal("101"), "RUB")
    order_book = OrderBookSnapshot(
        bids=(OrderBookLevel(Decimal("101.2"), 10),),
        asks=(OrderBookLevel(Decimal("101.3"), 10),),
        captured_at=NOW,
    )

    bundle = _run(
        evaluator.evaluate(
            command=_command(),
            position=position,
            order_book=order_book,
            history=_history(),
            lots=(),
            cycle=cycle,
            indicators=_indicators(),
            session_open=True,
            estimator=None,
        )
    )

    assert bundle.cycle.sell_armed is True
    assert bundle.cycle.pending_low == Decimal("101.2")
    assert bundle.cycle.last_sell_price == Decimal("102")
    assert isinstance(planner, _SpyPlanner)
    assert planner.last_cycle == bundle.cycle


def test_evaluator_rearms_cycle_and_observes_low_for_open_position() -> None:
    planner = _SpyPlanner()
    evaluator = PositionDecisionEvaluator(
        now=lambda: NOW,
        planner=planner,
    )
    cycle = TradingCycleState(
        automation_id="automation-eval",
        pending_low=None,
        last_buy_candle_at=None,
        sell_armed=False,
        last_sell_price=Decimal("100"),
        updated_at=NOW,
    )
    position = BrokerPosition("instrument-1", Decimal("2"), Decimal("100"), Decimal("101"), "RUB")
    order_book = OrderBookSnapshot(
        bids=(OrderBookLevel(Decimal("99"), 10),),
        asks=(OrderBookLevel(Decimal("99.2"), 10),),
        captured_at=NOW,
    )

    bundle = _run(
        evaluator.evaluate(
            command=_command(),
            position=position,
            order_book=order_book,
            history=_history(),
            lots=(),
            cycle=cycle,
            indicators=_indicators(),
            session_open=True,
            estimator=None,
        )
    )

    assert bundle.cycle.sell_armed is True
    assert bundle.cycle.pending_low == Decimal("99")


def test_range_position_is_none_without_bounds() -> None:
    evaluator = PositionDecisionEvaluator(now=lambda: NOW)
    indicators = _indicators().model_copy(update={"range_low": None, "range_high": None})
    assert (
        evaluator._range_position(Decimal("100"), TradingCycleState("a", None, None, False, None, NOW), indicators)
        is None
    )


def test_range_position_is_calculated_for_valid_bounds() -> None:
    result = PositionDecisionEvaluator(now=lambda: NOW)._range_position(
        Decimal("100"),
        TradingCycleState("a", None, None, True, None, NOW),
        _indicators(),
    )
    assert result == Decimal("0.5")
