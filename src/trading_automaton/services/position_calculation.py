"""Deterministic Decimal accounting for an automation position."""

from decimal import Decimal

from sentinel_contracts.broker_execution import OrderSide
from trading_automaton.domain.dtos import CalculatedPosition


class PositionCalculationService:
    def apply_execution(
        self,
        current: CalculatedPosition | None,
        *,
        side: OrderSide,
        executed_lots: int,
        lot_size: int,
        price: Decimal,
        commission: Decimal,
        current_price: Decimal,
    ) -> CalculatedPosition:
        if executed_lots <= 0 or lot_size <= 0 or price <= 0 or commission < 0:
            raise ValueError("Execution values are outside the allowed range.")
        previous = current or CalculatedPosition(
            quantity_units=0,
            average_price=Decimal(),
            invested_amount=Decimal(),
            realized_pnl=Decimal(),
            unrealized_pnl=Decimal(),
            actual_commissions=Decimal(),
            net_pnl=Decimal(),
        )
        executed_units = executed_lots * lot_size
        commissions = previous.actual_commissions + commission
        realized = previous.realized_pnl
        if side is OrderSide.BUY:
            quantity = previous.quantity_units + executed_units
            gross = previous.invested_amount + price * executed_units
            average = gross / quantity
        else:
            if executed_units > previous.quantity_units:
                raise ValueError("Sell execution exceeds the open quantity.")
            quantity = previous.quantity_units - executed_units
            realized += (price - previous.average_price) * executed_units
            average = previous.average_price if quantity else Decimal()
            gross = average * quantity
        unrealized = (current_price - average) * quantity
        return CalculatedPosition(
            quantity_units=quantity,
            average_price=average,
            invested_amount=gross,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            actual_commissions=commissions,
            net_pnl=realized + unrealized - commissions,
        )
