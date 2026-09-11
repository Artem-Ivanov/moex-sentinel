"""Construct strategy inputs from current broker facts and durable intent history."""

from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookSnapshot
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import CommissionSchedule, DecisionContext, MarketIndicators
from trading_automaton.storage.repository import (
    IntentHistory,
    TradeLotRecord,
    TradingCycleState,
)


class DecisionContextService:
    def __init__(self, settings: StrategySettings | None = None) -> None:
        self._settings = settings or StrategySettings()

    def build(
        self,
        command: AutomationCommand,
        position: BrokerPosition,
        order_book: OrderBookSnapshot,
        history: IntentHistory,
        *,
        commission_schedule: CommissionSchedule,
        averaging_step_percent: Decimal | None = None,
        minimum_net_profit_percent: Decimal | None = None,
        lots: tuple[TradeLotRecord, ...] = (),
        cycle: TradingCycleState | None = None,
        indicators: MarketIndicators | None = None,
        free_cash: Decimal = Decimal(),
        reserved_cash: Decimal = Decimal(),
    ) -> DecisionContext:
        integral_lots = position.quantity_lots.to_integral_value()
        if position.quantity_lots != integral_lots or integral_lots < 0:
            raise ValueError("Broker position quantity must contain whole non-negative lots.")
        quantity_lots = int(integral_lots)
        average_price = position.average_price
        return DecisionContext(
            core_available=True,
            has_active_intent=False,
            quantity_lots=quantity_lots,
            lot_size=command.lot_size,
            average_price=average_price,
            current_price=order_book.best_bid.price,
            best_bid=order_book.best_bid.price,
            best_ask=order_book.best_ask.price,
            last_buy_price=history.last_buy_price or average_price,
            invested_amount=average_price * command.lot_size * quantity_lots,
            commission_schedule=commission_schedule,
            buy_commission_per_lot=(
                history.buy_commissions / history.total_bought_lots if history.total_bought_lots > 0 else Decimal()
            ),
            averaging_step_percent=(
                averaging_step_percent if averaging_step_percent is not None else self._settings.averaging_step_percent
            ),
            minimum_net_profit_percent=(
                minimum_net_profit_percent
                if minimum_net_profit_percent is not None
                else self._settings.partial_take_profit_percent
            ),
            completed_partial_sell_steps=history.completed_partial_sell_steps,
            settings=self._settings,
            currency=command.currency,
            min_price_increment=command.min_price_increment,
            lots=lots,
            cycle=cycle,
            indicators=indicators,
            free_cash=free_cash,
            reserved_cash=reserved_cash,
            available_free_cash=max(Decimal(), free_cash - reserved_cash),
            required_order_cash=(
                order_book.best_ask.price * command.lot_size * self._settings.buy_order_lots
                + commission_schedule.estimate(
                    "BUY",
                    order_book.best_ask.price * command.lot_size * self._settings.buy_order_lots,
                )
            ),
        )
