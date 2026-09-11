"""Parallel pure decision preparation for one immutable market snapshot."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    MarketBatchSnapshot,
)
from sentinel_contracts.trading import DecisionKind
from trading_automaton.domain.dtos import PositionEvaluationResult, PositionWorkItem, PreparedDecision, TradeDecision
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.broker_rate_limit import BrokerRateLimitService
from trading_automaton.services.order_book_validation import OrderBookValidationService

if TYPE_CHECKING:
    pass


class PositionDecisionPort(Protocol):
    async def decide(
        self,
        item: PositionWorkItem,
        state: InstrumentMarketState,
    ) -> PositionEvaluationResult: ...


class PositionBatchSchedulerService:
    def __init__(
        self,
        decider: PositionDecisionPort,
        *,
        max_order_book_age: timedelta = timedelta(seconds=2),
        rate_limit: BrokerRateLimitService | None = None,
        order_books: OrderBookValidationService | None = None,
        active_intents: ActiveIntentGateService | None = None,
    ) -> None:
        self._decider = decider
        self._max_order_book_age = max_order_book_age
        self._rate_limit = rate_limit
        self._order_books = order_books or OrderBookValidationService()
        self._active_intents = active_intents
        self._locks: dict[str, asyncio.Lock] = {}
        self._generations: dict[str, int] = {}

    async def prepare(
        self,
        items: tuple[PositionWorkItem, ...],
        snapshot: MarketBatchSnapshot,
    ) -> tuple[PreparedDecision, ...]:
        tasks: list[asyncio.Task[PreparedDecision]] = []
        async with asyncio.TaskGroup() as group:
            for item in items:
                automation_id = str(item.command.automation_id)
                generation = self._generations.get(automation_id, 0) + 1
                self._generations[automation_id] = generation
                tasks.append(group.create_task(self._prepare_one(item, snapshot, generation)))
        return tuple(task.result() for task in tasks)

    async def _prepare_one(
        self,
        item: PositionWorkItem,
        snapshot: MarketBatchSnapshot,
        generation: int,
    ) -> PreparedDecision:
        automation_id = str(item.command.automation_id)
        lock = self._locks.setdefault(automation_id, asyncio.Lock())
        async with lock:
            if generation != self._generations[automation_id]:
                return self._result(item, snapshot, "SUPERSEDED_SNAPSHOT")
            state = snapshot.instruments.get(item.command.external_instrument_id)
            if state is None or state.order_book is None or not state.order_book.is_consistent:
                return self._result(item, snapshot, "ORDER_BOOK_UNAVAILABLE")
            order_book = OrderBookSnapshot(
                state.order_book.bids,
                state.order_book.asks,
                state.order_book.captured_at,
            )
            validation = self._order_books.validate(
                order_book,
                now=snapshot.created_at,
                max_age=self._max_order_book_age,
            )
            if not validation.valid:
                return self._result(item, snapshot, validation.reason_code or "INVALID_ORDER_BOOK")
            if state.trading_status is None:
                return self._result(item, snapshot, "TRADING_STATUS_UNAVAILABLE")
            if item.has_active_intent or (
                self._active_intents is not None and self._active_intents.has_active_intent(automation_id)
            ):
                return self._result(item, snapshot, "ACTIVE_INTENT")
            if not item.commission_profile_available:
                return self._result(item, snapshot, "COMMISSION_PROFILE_UNAVAILABLE")
            if not (state.trading_status.limit_order_available and state.trading_status.api_trade_available):
                return self._result(item, snapshot, "MARKET_SESSION_CLOSED")
            try:
                evaluation = await self._decider.decide(item, state)
            except Exception:
                return self._result(item, snapshot, "POSITION_PROCESSING_FAILED")
            if (
                evaluation.decision.kind in {DecisionKind.BUY_MORE, DecisionKind.SELL_PART, DecisionKind.SELL_ALL}
                and self._rate_limit is not None
                and not self._rate_limit.try_acquire(snapshot.created_at)
            ):
                return self._result(item, snapshot, "BROKER_RATE_LIMIT_BUDGET")
            return PreparedDecision(
                item.command,
                snapshot.snapshot_id,
                snapshot.created_at,
                evaluation,
            )

    @staticmethod
    def _result(
        item: PositionWorkItem,
        snapshot: MarketBatchSnapshot,
        reason: str,
    ) -> PreparedDecision:
        return PreparedDecision(
            item.command,
            snapshot.snapshot_id,
            snapshot.created_at,
            PositionEvaluationResult(
                TradeDecision(DecisionKind.WAIT, 0, None, reason),
                item.state,
                Decimal(),
            ),
        )
