"""Compose cycle transitions and trading decisions for one hydrated position."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookSnapshot
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import MarketIndicators
from trading_automaton.domain.storage_dtos import (
    IntentHistory,
    TradeLotRecord,
    TradingCycleState,
)
from trading_automaton.services.runtime_decision_planner import (
    DecisionEstimatePort,
    DecisionPlan,
    TradingDecisionPlanner,
)
from trading_automaton.services.trading_cycle import TradingCycleService


class PositionDecisionBundle(PositionalModel):
    """Result of one decision evaluation cycle for a single automation."""

    model_config = ConfigDict(frozen=True)

    plan: DecisionPlan
    cycle: TradingCycleState
    range_position: Decimal | None


class PositionDecisionEvaluator:
    """Build cycle state and derive a single trading decision from a snapshot."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        planner: TradingDecisionPlanner | None = None,
        cycles: TradingCycleService | None = None,
    ) -> None:
        self._planner = planner or TradingDecisionPlanner(now=now)
        self._now = now
        self._cycles = cycles or TradingCycleService()

    async def evaluate(
        self,
        *,
        command: AutomationCommand,
        position: BrokerPosition,
        order_book: OrderBookSnapshot,
        history: IntentHistory,
        lots: tuple[TradeLotRecord, ...],
        cycle: TradingCycleState,
        indicators: MarketIndicators,
        session_open: bool,
        estimator: DecisionEstimatePort | None,
    ) -> PositionDecisionBundle:
        observed_cycle = self._observe_cycle(command, position, order_book, history, cycle, indicators)
        plan = await self._planner.plan(
            command=command,
            position=position,
            order_book=order_book,
            history=history,
            lots=lots,
            cycle=observed_cycle,
            indicators=indicators,
            session_open=session_open,
            estimator=estimator,
        )
        return PositionDecisionBundle(
            plan=plan,
            cycle=observed_cycle,
            range_position=self._range_position(order_book.best_ask.price, observed_cycle, indicators),
        )

    def _observe_cycle(
        self,
        command: AutomationCommand,
        position: BrokerPosition,
        order_book: OrderBookSnapshot,
        history: IntentHistory,
        cycle: TradingCycleState,
        indicators: MarketIndicators,
    ) -> TradingCycleState:
        if position.quantity_lots == 0:
            return self._cycles.observe_flat_entry(
                cycle,
                order_book.best_bid.price,
                indicators.candle_direction,
                indicators.averaging_step_percent,
                now=self._now(),
            )
        cycle = self._cycles.rearm_for_next_sell(
            cycle,
            order_book.best_bid.price,
            retracement_percent=indicators.averaging_step_percent,
            progression_percent=indicators.minimum_net_profit_percent,
            now=self._now(),
        )
        trigger = (history.last_buy_price or position.average_price) * (
            Decimal("1") - indicators.averaging_step_percent / 100
        )
        if order_book.best_bid.price <= trigger:
            return self._cycles.observe_low(cycle, order_book.best_bid.price, now=self._now())
        return cycle

    @staticmethod
    def _range_position(
        price: Decimal,
        cycle: TradingCycleState,
        indicators: MarketIndicators,
    ) -> Decimal | None:
        if (
            indicators.range_low is not None
            and indicators.range_high is not None
            and indicators.range_high > indicators.range_low
        ):
            return (price - indicators.range_low) / (indicators.range_high - indicators.range_low)
        return None
