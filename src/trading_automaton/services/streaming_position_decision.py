"""Pure position decision over state hydrated before the market snapshot."""

import asyncio
from collections.abc import Callable
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
from trading_automaton.services.decision import TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.storage.repository import (
    AccountCommissionProfileKey,
)


class HydratedPositionStatePort(Protocol):
    async def get(self, automation_id: str) -> HydratedPositionState | None: ...


class CachedCommissionPort(Protocol):
    def schedule(
        self,
        key: AccountCommissionProfileKey,
        *,
        snapshot_at: datetime,
    ) -> CommissionSchedule | None: ...


class PositionDecisionPort(Protocol):
    def decide(self, context: object) -> TradeDecision: ...


class DecisionCashPort(Protocol):
    async def available(self, account_id: str, currency: str) -> Decimal: ...

    async def reserved(self, account_id: str, currency: str) -> Decimal: ...


class StreamingPositionDecisionService:
    def __init__(
        self,
        states: HydratedPositionStatePort,
        commissions: CachedCommissionPort,
        *,
        cash: DecisionCashPort | None = None,
        decisions: PositionDecisionPort | None = None,
        contexts: DecisionContextService | None = None,
        now: Callable[[], datetime],
    ) -> None:
        self._states = states
        self._commissions = commissions
        self._decisions = decisions or TradeDecisionService()
        self._cash = cash
        self._contexts = contexts or DecisionContextService()
        self._now = now

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
        context = self._build(item, hydrated, order_book, schedule, free_cash, reserved_cash)
        decision = self._decisions.decide(context)
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
        )
