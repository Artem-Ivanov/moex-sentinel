"""Decision planning service for trading automaton runtime iteration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from pydantic import ConfigDict

from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerPosition, LimitOrderEstimate, OrderBookSnapshot, OrderSide
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import (
    AdaptiveThresholds,
    CommissionSchedule,
    DecisionContext,
    MarketIndicators,
    TradeDecision,
)
from trading_automaton.domain.storage_dtos import (
    IntentHistory,
    TradeLotRecord,
    TradingCycleState,
)
from trading_automaton.services.decision import TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.market_indicators import MarketIndicatorsService
from trading_automaton.services.volatility_strategy import VolatilityThresholdCache


class CandleDataPort(Protocol):
    """Port for fetching historic candles used to derive market indicators."""

    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]: ...


class DecisionEstimatePort(Protocol):
    async def estimate_limit_order(
        self,
        account_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity_lots: int,
        price: Decimal,
    ) -> LimitOrderEstimate: ...


class DecisionPlan(PositionalModel):
    model_config = ConfigDict(frozen=True)

    indicators: MarketIndicators
    context: DecisionContext
    decision: TradeDecision
    estimated_commission: Decimal = Decimal()
    context_after_estimate: DecisionContext | None = None

    @property
    def effective_context(self) -> DecisionContext:
        return self.context_after_estimate or self.context


class TradingDecisionPlanner:
    """Isolated layer for decision context, indicator loading and pre-flight estimation."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        settings: StrategySettings | None = None,
        decisions: TradeDecisionService | None = None,
        contexts: DecisionContextService | None = None,
        indicators_service: MarketIndicatorsService | None = None,
        threshold_cache: VolatilityThresholdCache | None = None,
    ) -> None:
        self._now = now
        self._settings = settings or StrategySettings()
        self._decisions = decisions or TradeDecisionService()
        self._contexts = contexts or DecisionContextService(self._settings)
        self._indicators_service = indicators_service or MarketIndicatorsService()
        self._threshold_cache = threshold_cache or VolatilityThresholdCache(now=now)

    @property
    def strategy_code(self) -> str:
        return self._decisions.strategy_code

    @property
    def strategy_version(self) -> str:
        return self._decisions.strategy_version

    async def load_market_indicators(
        self,
        command: AutomationCommand,
        candles: CandleDataPort | None = None,
    ) -> MarketIndicators:
        fallback = AdaptiveThresholds(
            self._settings.averaging_step_percent,
            self._settings.partial_take_profit_percent,
            "FALLBACK",
        )
        if candles is None:
            return self._indicators_service.calculate((), fallback)

        async def load() -> MarketIndicators:
            end = self._now()
            payload = await candles.get_candles(
                command.external_instrument_id,
                end - timedelta(hours=2),
                end,
                CandleInterval.MIN_1,
            )
            return self._indicators_service.calculate(payload, fallback)

        return await self._threshold_cache.get_or_load(str(command.broker_id), command.external_instrument_id, load)

    async def plan(
        self,
        command: AutomationCommand,
        position: BrokerPosition,
        order_book: OrderBookSnapshot,
        history: IntentHistory,
        lots: tuple[TradeLotRecord, ...],
        cycle: TradingCycleState,
        indicators: MarketIndicators,
        session_open: bool,
        estimator: DecisionEstimatePort | None,
    ) -> DecisionPlan:
        if not session_open:
            context = self._contexts.build(
                command,
                position,
                order_book,
                history,
                commission_schedule=CommissionSchedule(Decimal(), Decimal()),
                averaging_step_percent=indicators.averaging_step_percent,
                minimum_net_profit_percent=indicators.minimum_net_profit_percent,
                lots=lots,
                cycle=cycle,
                indicators=indicators,
            )
            return DecisionPlan(
                indicators=indicators,
                context=context,
                decision=TradeDecision(
                    kind=DecisionKind.WAIT,
                    quantity_lots=0,
                    limit_price=None,
                    reason_code="MARKET_SESSION_CLOSED",
                ),
            )
        context = self._contexts.build(
            command,
            position,
            order_book,
            history,
            commission_schedule=CommissionSchedule(Decimal(), Decimal()),
            averaging_step_percent=indicators.averaging_step_percent,
            minimum_net_profit_percent=indicators.minimum_net_profit_percent,
            lots=lots,
            cycle=cycle,
            indicators=indicators,
        )
        decision = self._decisions.decide(context)
        if estimator is None or decision.kind not in {
            DecisionKind.BUY_MORE,
            DecisionKind.SELL_PART,
            DecisionKind.SELL_ALL,
        }:
            return DecisionPlan(indicators=indicators, context=context, decision=decision)
        if decision.limit_price is None:
            raise RuntimeError("Executable decision requires a limit price.")
        side = OrderSide.BUY if decision.kind is DecisionKind.BUY_MORE else OrderSide.SELL
        estimate = await estimator.estimate_limit_order(
            command.account_id,
            command.external_instrument_id,
            side,
            decision.quantity_lots,
            decision.limit_price,
        )
        estimated_commission = estimate.estimated_commission
        amount = decision.limit_price * command.lot_size * decision.quantity_lots
        rate = estimated_commission / amount if amount > 0 else Decimal()
        schedule = CommissionSchedule(rate, Decimal()) if side is OrderSide.BUY else CommissionSchedule(Decimal(), rate)
        estimated_context = self._contexts.build(
            command,
            position,
            order_book,
            history,
            commission_schedule=schedule,
            averaging_step_percent=indicators.averaging_step_percent,
            minimum_net_profit_percent=indicators.minimum_net_profit_percent,
            lots=lots,
            cycle=cycle,
            indicators=indicators,
        )
        estimated_decision = self._decisions.decide(estimated_context)
        return DecisionPlan(
            indicators=indicators,
            context=context,
            decision=estimated_decision,
            estimated_commission=estimated_commission,
            context_after_estimate=estimated_context,
        )
