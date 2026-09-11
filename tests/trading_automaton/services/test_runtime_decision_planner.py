"""Unit tests for runtime decision planner service."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle
from sentinel_contracts.broker_execution import (
    BrokerPosition,
    LimitOrderEstimate,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
)
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import MarketIndicators
from trading_automaton.domain.storage_dtos import IntentHistory, TradeLotRecord, TradingCycleState
from trading_automaton.services.runtime_decision_planner import (
    DecisionEstimatePort,
    TradeDecision,
    TradingDecisionPlanner,
)

NOW = datetime(2026, 8, 10, 10, tzinfo=UTC)


def _command() -> AutomationCommand:
    return baseline_command(
        automation="automation-1",
        broker="broker-1",
        account="account-1",
        instrument="instrument-1",
    )


def _order_book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        bids=(OrderBookLevel(Decimal("99.8"), 10),),
        asks=(OrderBookLevel(Decimal("100"), 10),),
        captured_at=NOW,
    )


def _history() -> IntentHistory:
    return IntentHistory(
        last_buy_price=Decimal("100"),
        completed_partial_sell_steps=0,
        actual_commissions=Decimal(),
        total_bought_lots=0,
        buy_commissions=Decimal(),
    )


def _cycle() -> TradingCycleState:
    return TradingCycleState(
        automation_id="automation-1",
        pending_low=None,
        last_buy_candle_at=None,
        sell_armed=True,
        last_sell_price=None,
        updated_at=NOW,
    )


def _indicators_fallback() -> MarketIndicators:
    return MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="FALLBACK",
        mean_5=None,
        mean_20=None,
        change_10_percent=None,
        last_candle_at=NOW,
        last_candle_open=Decimal("100"),
        last_candle_close=Decimal("100"),
        range_low=Decimal("95"),
        range_high=Decimal("105"),
    )


def _lots() -> tuple[TradeLotRecord, ...]:
    return (
        TradeLotRecord(
            "lot-1",
            "automation-1",
            "intent-1",
            "reconciled",
            1,
            1,
            Decimal("100"),
            Decimal("0"),
            NOW,
            None,
        ),
    )


def _run_async(task: object) -> object:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(task)
    finally:
        loop.close()


class _DecisionSequence:
    code = "SEQ"
    version = "1"

    def __init__(self, sequence: tuple[TradeDecision, ...]) -> None:
        self.sequence = list(sequence)
        self.calls = 0

    def decide(self, context: object) -> TradeDecision:  # noqa: ARG002
        self.calls += 1
        if not self.sequence:
            return TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="NO_DECISION",
            )
        return self.sequence.pop(0)


class _WaitDecision:
    def __init__(self) -> None:
        self.code = "WAIT"
        self.version = "1"
        self.calls = 0

    def decide(self, context: object) -> TradeDecision:  # noqa: ARG002
        self.calls += 1
        return TradeDecision(
            kind=DecisionKind.WAIT,
            quantity_lots=0,
            limit_price=None,
            reason_code="WAIT_STRATEGY",
        )


class _SpyEstimator:
    def __init__(self) -> None:
        self.calls = 0

    async def estimate_limit_order(
        self,
        account_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity_lots: int,
        price: Decimal,
    ) -> LimitOrderEstimate:  # noqa: ARG002
        self.calls += 1
        assert account_id == "account-1"
        assert instrument_id == "instrument-1"
        assert side is OrderSide.BUY
        assert quantity_lots == 1
        assert price == Decimal("100")
        return LimitOrderEstimate(
            total_amount=price * quantity_lots * Decimal("10"),
            estimated_commission=Decimal("2.5"),
            currency="RUB",
        )


class _SpyCandleData:
    def __init__(self, candles: tuple[HistoricCandle, ...]) -> None:
        self.candles = candles
        self.calls = 0

    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]:  # noqa: ARG001
        self.calls += 1
        assert instrument_id == "instrument-1"
        assert interval == CandleInterval.MIN_1
        assert start < end
        return self.candles


def _build_complete_candles() -> tuple[HistoricCandle, ...]:
    return tuple(
        HistoricCandle(
            instrument_id="instrument-1",
            open=Decimal("100") + Decimal(i) / Decimal("10"),
            high=Decimal("100") + Decimal(i) / Decimal("10"),
            low=Decimal("100") + Decimal(i) / Decimal("10"),
            close=Decimal("100") + Decimal(i) / Decimal("10"),
            volume=100,
            started_at=NOW - timedelta(minutes=16 - i),
            is_complete=True,
        )
        for i in range(16)
    )


def test_wait_when_market_closed_does_not_call_estimate() -> None:
    planner = TradingDecisionPlanner(
        now=lambda: NOW,
        decisions=_WaitDecision(),
    )
    estimator: DecisionEstimatePort = _SpyEstimator()

    result = _run_async(
        planner.plan(
            command=_command(),
            position=BrokerPosition("instrument-1", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
            order_book=_order_book(),
            history=_history(),
            lots=_lots(),
            cycle=_cycle(),
            indicators=_indicators_fallback(),
            session_open=False,
            estimator=estimator,
        )
    )

    assert result.decision.kind is DecisionKind.WAIT
    assert result.decision.reason_code == "MARKET_SESSION_CLOSED"
    assert result.estimated_commission == Decimal()
    assert estimator.calls == 0


def test_wait_plan_skips_estimate() -> None:
    decisions = _WaitDecision()
    planner = TradingDecisionPlanner(now=lambda: NOW, decisions=decisions)
    estimator: DecisionEstimatePort = _SpyEstimator()

    result = _run_async(
        planner.plan(
            command=_command(),
            position=BrokerPosition("instrument-1", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
            order_book=_order_book(),
            history=_history(),
            lots=_lots(),
            cycle=_cycle(),
            indicators=_indicators_fallback(),
            session_open=True,
            estimator=estimator,
        )
    )

    assert result.decision.kind is DecisionKind.WAIT
    assert result.decision.reason_code == "WAIT_STRATEGY"
    assert estimator.calls == 0


def test_actionable_decision_is_recomputed_with_estimated_commission() -> None:
    decisions = _DecisionSequence(
        (
            TradeDecision(
                kind=DecisionKind.BUY_MORE,
                quantity_lots=1,
                limit_price=Decimal("100"),
                reason_code="INITIAL_DECISION",
            ),
            TradeDecision(
                kind=DecisionKind.WAIT,
                quantity_lots=0,
                limit_price=None,
                reason_code="AFTER_ESTIMATE",
            ),
        )
    )
    planner = TradingDecisionPlanner(now=lambda: NOW, decisions=decisions)
    estimator: DecisionEstimatePort = _SpyEstimator()

    result = _run_async(
        planner.plan(
            command=_command(),
            position=BrokerPosition("instrument-1", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
            order_book=_order_book(),
            history=_history(),
            lots=_lots(),
            cycle=_cycle(),
            indicators=_indicators_fallback(),
            session_open=True,
            estimator=estimator,
        )
    )

    assert decisions.calls == 2
    assert estimator.calls == 1
    assert result.decision.kind is DecisionKind.WAIT
    assert result.decision.reason_code == "AFTER_ESTIMATE"
    assert result.estimated_commission == Decimal("2.5")
    assert result.context_after_estimate is not None
    estimated_order_amount = Decimal("1000")
    assert result.context_after_estimate.commission_schedule.buy_rate == Decimal("2.5") / estimated_order_amount
    assert result.context_after_estimate.commission_schedule.sell_rate == Decimal()


def test_load_market_indicators_without_candle_data_returns_fallback() -> None:
    settings = StrategySettings(
        STRATEGY_AVERAGING_STEP_PERCENT="0.75",
        STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT="0.8",
    )
    planner = TradingDecisionPlanner(now=lambda: NOW, settings=settings)

    result = _run_async(planner.load_market_indicators(command=_command(), candles=None))

    assert result.source == "FALLBACK"
    assert result.averaging_step_percent == Decimal("0.75")
    assert result.minimum_net_profit_percent == Decimal("0.8")


def test_load_market_indicators_cached_for_same_broker_instrument() -> None:
    candles = _build_complete_candles()
    spy = _SpyCandleData(candles)

    planner = TradingDecisionPlanner(now=lambda: NOW)
    command = _command()

    first = _run_async(planner.load_market_indicators(command, spy))
    second = _run_async(planner.load_market_indicators(command, spy))

    assert spy.calls == 1
    assert first.source == "ADAPTIVE"
    assert second.source == "ADAPTIVE"
    assert first.averaging_step_percent == second.averaging_step_percent
