"""Pure position decision over state hydrated before the market snapshot."""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import InstrumentMarketState
from sentinel_contracts.trading import DecisionKind
from trading_automaton.domain.dtos import (
    CommissionSchedule,
    DecisionContext,
    HydratedPositionState,
    PositionEvaluationResult,
    PositionWorkItem,
    TradeDecision,
)
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfileKey,
)
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.order_quota import apply_order_quota


class CachedCommissionPort(Protocol):
    def schedule(
        self,
        key: AccountCommissionProfileKey,
        *,
        snapshot_at: datetime,
    ) -> CommissionSchedule | None: ...


class PositionDecisionPort(Protocol):
    def decide(self, context: DecisionContext) -> TradeDecision: ...


class DecisionCashPort(Protocol):
    async def available(self, account_id: str, currency: str) -> Decimal: ...

    async def reserved(self, account_id: str, currency: str) -> Decimal: ...


class PositionCountPort(Protocol):
    async def positive_security_count(self, account_id: str) -> int: ...


class StreamingPositionDecisionService:
    """Evaluate hydrated positions with explicit decision and context dependencies."""

    def __init__(
        self,
        commissions: CachedCommissionPort,
        *,
        cash: DecisionCashPort | None = None,
        positions: PositionCountPort | None = None,
        decisions: PositionDecisionPort,
        contexts: DecisionContextService,
    ) -> None:
        """Use the work item's state and snapshot time with the supplied decision dependencies."""
        self._commissions = commissions
        self._decisions = decisions
        self._cash = cash
        self._positions = positions
        self._contexts = contexts

    async def decide(
        self,
        item: PositionWorkItem,
        market: InstrumentMarketState,
    ) -> PositionEvaluationResult:
        hydrated = item.state
        if hydrated is None or market.order_book is None:
            return self._result(hydrated, "POSITION_STATE_UNAVAILABLE")
        order_book = OrderBookSnapshot(
            market.order_book.bids,
            market.order_book.asks,
            market.order_book.captured_at,
        )
        available_cash = Decimal()
        reserved_cash = Decimal()
        if self._cash is not None:
            available_cash, reserved_cash = await asyncio.gather(
                self._cash.available(item.command.account_id, item.command.currency),
                self._cash.reserved(item.command.account_id, item.command.currency),
            )
        free_cash = available_cash + reserved_cash
        if item.snapshot_at is None:
            return self._result(hydrated, "SNAPSHOT_TIME_UNAVAILABLE")
        schedule = self._commissions.schedule(
            AccountCommissionProfileKey(
                str(item.command.broker_id),
                item.command.account_id,
                item.instrument_type,
                item.command.currency,
            ),
            snapshot_at=item.snapshot_at,
        )
        if schedule is None:
            return self._result(hydrated, "COMMISSION_PROFILE_UNAVAILABLE")
        position_count = (
            await self._positions.positive_security_count(item.command.account_id)
            if item.command.order_quota_percent is not None and self._positions is not None
            else None
        )
        context = self._build(
            item,
            hydrated,
            order_book,
            schedule,
            free_cash,
            reserved_cash,
            position_count,
        )
        decision = self._decisions.decide(context)
        decision = apply_order_quota(
            decision,
            item.command,
            available_free_cash=context.available_free_cash,
            positive_position_count=position_count,
            best_bid=order_book.best_bid.price,
            best_ask=order_book.best_ask.price,
        )
        if decision.kind not in {
            DecisionKind.BUY_MORE,
            DecisionKind.SELL_PART,
            DecisionKind.SELL_ALL,
        }:
            return PositionEvaluationResult(decision, hydrated, Decimal())
        if decision.limit_price is None:
            return self._result(hydrated, "LIMIT_PRICE_UNAVAILABLE")
        side = "BUY" if decision.kind is DecisionKind.BUY_MORE else "SELL"
        amount = decision.limit_price * item.command.lot_size * decision.quantity_lots
        return PositionEvaluationResult(decision, hydrated, schedule.estimate(side, amount))

    @staticmethod
    def _result(hydrated: HydratedPositionState | None, reason: str) -> PositionEvaluationResult:
        return PositionEvaluationResult(TradeDecision(DecisionKind.WAIT, 0, None, reason), hydrated, Decimal())

    def _build(
        self,
        item: PositionWorkItem,
        hydrated: HydratedPositionState,
        order_book: OrderBookSnapshot,
        schedule: CommissionSchedule,
        free_cash: Decimal,
        reserved_cash: Decimal,
        positive_position_count: int | None,
    ) -> DecisionContext:
        indicators = hydrated.indicators
        return self._contexts.build(
            item.command,
            hydrated.position,
            order_book,
            hydrated.history,
            commission_schedule=schedule,
            averaging_step_percent=indicators.averaging_step_percent,
            minimum_net_profit_percent=indicators.minimum_net_profit_percent,
            lots=hydrated.lots,
            cycle=hydrated.cycle,
            indicators=indicators,
            free_cash=free_cash,
            reserved_cash=reserved_cash,
            positive_position_count=positive_position_count,
        )
