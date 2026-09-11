"""Apply one fresh market event to the durable scalping-cycle state."""

from collections.abc import Callable
from datetime import datetime, timedelta

from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import InstrumentMarketState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import HydratedPositionState
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.trading_cycle import TradingCycleService


class StreamingCycleTransitionService:
    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        cycles: TradingCycleService | None = None,
        max_order_book_age: timedelta = timedelta(seconds=2),
    ) -> None:
        self._now = now
        self._cycles = cycles or TradingCycleService()
        self._order_books = OrderBookValidationService()
        self._max_order_book_age = max_order_book_age

    def apply(
        self,
        command: AutomationCommand,
        state: HydratedPositionState,
        market: InstrumentMarketState,
        *,
        snapshot_at: datetime | None = None,
    ) -> HydratedPositionState:
        order_book = market.order_book
        if order_book is None or not order_book.is_consistent:
            return state
        now = snapshot_at if snapshot_at is not None else self._now()
        if not self._order_books.validate(
            OrderBookSnapshot(order_book.bids, order_book.asks, order_book.captured_at),
            now=now,
            max_age=self._max_order_book_age,
        ).valid:
            return state
        price = order_book.best_bid.price
        indicators = state.indicators
        cycle = state.cycle
        if state.position.quantity_lots <= 0:
            cycle = self._cycles.observe_flat_entry(
                cycle,
                price,
                indicators.candle_direction,
                indicators.averaging_step_percent,
                now=now,
            )
        else:
            cycle = self._cycles.rearm_for_next_sell(
                cycle,
                price,
                retracement_percent=indicators.averaging_step_percent,
                progression_percent=indicators.minimum_net_profit_percent,
                now=now,
            )
            trigger = (state.history.last_buy_price or state.position.average_price) * (
                1 - indicators.averaging_step_percent / 100
            )
            if price <= trigger:
                cycle = self._cycles.observe_low(cycle, price, now=now)
        return state.model_copy(update={"cycle": cycle})
