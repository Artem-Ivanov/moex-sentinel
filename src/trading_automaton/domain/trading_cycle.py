"""Pure execution transitions shared by cycle services and atomic persistence."""

from datetime import datetime
from decimal import Decimal

from trading_automaton.domain.storage_dtos import TradingCycleState


def mark_buy(state: TradingCycleState, candle_at: datetime, *, now: datetime) -> TradingCycleState:
    """Clear the pending low and record the caller-selected buy candle and update time."""
    return state.model_copy(update={"pending_low": None, "last_buy_candle_at": candle_at, "updated_at": now})


def mark_sell(state: TradingCycleState, price: Decimal, *, now: datetime) -> TradingCycleState:
    """Clear the pending low and disarm selling at the executed unit price."""
    return state.model_copy(
        update={
            "pending_low": None,
            "sell_armed": False,
            "last_sell_price": price,
            "updated_at": now,
        }
    )
