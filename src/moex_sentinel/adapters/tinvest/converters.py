"""SDK value conversion isolated from business layers."""

from decimal import Decimal
from typing import Any

from moex_sentinel.domain.portfolio import Money

NANO_FACTOR = Decimal("1000000000")


def quotation_to_decimal(value: Any) -> Decimal:
    return Decimal(value.units) + Decimal(value.nano) / NANO_FACTOR


def money_to_domain(value: Any | None) -> Money | None:
    if value is None:
        return None
    return Money(quotation_to_decimal(value), str(value.currency).upper())


def enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


def execution_unit_price(response: Any, *, order_state: bool) -> Decimal:
    """Keep OrderState's aggregate executed_order_price out of unit-price accounting.

    PostOrderResponse.executed_order_price is per instrument. GetOrderState/GetOrders
    instead expose that price as average_position_price (or individual stage prices).
    """
    if response.lots_executed <= 0:
        return Decimal()
    field = "average_position_price" if order_state else "executed_order_price"
    value = getattr(response, field, None)
    if value is not None:
        price = quotation_to_decimal(value)
        if price.is_finite() and price > 0:
            return price
    if order_state:
        stages = getattr(response, "stages", ())
        weighted = Decimal()
        quantity = 0
        for stage in stages:
            stage_price = getattr(stage, "price", None)
            stage_quantity = getattr(stage, "quantity", 0)
            if stage_price is None or stage_quantity <= 0:
                break
            price = quotation_to_decimal(stage_price)
            if not price.is_finite() or price <= 0:
                break
            weighted += price * stage_quantity
            quantity += stage_quantity
        else:
            if quantity == response.lots_executed:
                return weighted / quantity
    raise ValueError("Executed order unit price is unavailable.")
