"""Durable scalping-cycle transitions."""

from datetime import datetime
from decimal import Decimal

from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.domain.trading_cycle import mark_buy, mark_sell


class TradingCycleService:
    def complete_cycle(self, state: TradingCycleState, exit_price: Decimal, *, now: datetime) -> TradingCycleState:
        return state.model_copy(
            update={
                "pending_low": None,
                "sell_armed": False,
                "last_sell_price": exit_price,
                "updated_at": now,
            }
        )

    def observe_flat_entry(
        self,
        state: TradingCycleState,
        price: Decimal,
        direction: str,
        threshold_percent: Decimal,
        *,
        now: datetime,
    ) -> TradingCycleState:
        if direction == "GREEN" and not state.sell_armed:
            anchor = max(state.last_sell_price or price, price)
            pullback_price = anchor * (1 - threshold_percent / 100)
            if price > pullback_price:
                return state.model_copy(update={"last_sell_price": anchor, "updated_at": now})
            return state.model_copy(
                update={
                    "pending_low": price,
                    "sell_armed": True,
                    "last_sell_price": anchor,
                    "updated_at": now,
                }
            )
        low = price if state.pending_low is None else min(state.pending_low, price)
        return state.model_copy(update={"pending_low": low, "sell_armed": True, "updated_at": now})

    def observe_low(self, state: TradingCycleState, price: Decimal, *, now: datetime) -> TradingCycleState:
        low = price if state.pending_low is None else min(state.pending_low, price)
        return state.model_copy(update={"pending_low": low, "updated_at": now})

    def mark_buy(self, state: TradingCycleState, candle_at: datetime, *, now: datetime) -> TradingCycleState:
        """Apply the shared buy transition without persisting the returned state."""
        return mark_buy(state, candle_at, now=now)

    def mark_sell(self, state: TradingCycleState, price: Decimal, *, now: datetime) -> TradingCycleState:
        """Apply the shared sell transition without persisting the returned state."""
        return mark_sell(state, price, now=now)

    def rearm_for_next_sell(
        self,
        state: TradingCycleState,
        price: Decimal,
        *,
        retracement_percent: Decimal,
        progression_percent: Decimal,
        now: datetime,
    ) -> TradingCycleState:
        if state.sell_armed or state.last_sell_price is None:
            return state
        retracement_price = state.last_sell_price * (1 - retracement_percent / 100)
        progression_price = state.last_sell_price * (1 + progression_percent / 100)
        if price <= retracement_price or price >= progression_price:
            return state.model_copy(update={"pending_low": None, "sell_armed": True, "updated_at": now})
        return state
