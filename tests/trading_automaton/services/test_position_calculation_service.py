"""Decimal position accounting including actual broker commissions."""

from decimal import Decimal

from sentinel_contracts.broker_execution import OrderSide
from trading_automaton.services.position_calculation import PositionCalculationService


def test_open_average_partial_sell_and_net_pnl_are_hand_derived() -> None:
    service = PositionCalculationService()

    opened = service.apply_execution(
        None,
        side=OrderSide.BUY,
        executed_lots=1,
        lot_size=10,
        price=Decimal("100"),
        commission=Decimal("1"),
        current_price=Decimal("100"),
    )
    averaged = service.apply_execution(
        opened,
        side=OrderSide.BUY,
        executed_lots=1,
        lot_size=10,
        price=Decimal("80"),
        commission=Decimal("1"),
        current_price=Decimal("80"),
    )
    sold = service.apply_execution(
        averaged,
        side=OrderSide.SELL,
        executed_lots=1,
        lot_size=10,
        price=Decimal("110"),
        commission=Decimal("2"),
        current_price=Decimal("105"),
    )

    assert opened.quantity_units == 10
    assert opened.average_price == Decimal("100")
    assert averaged.quantity_units == 20
    assert averaged.average_price == Decimal("90")
    assert averaged.invested_amount == Decimal("1800")
    assert sold.quantity_units == 10
    assert sold.realized_pnl == Decimal("200")
    assert sold.unrealized_pnl == Decimal("150")
    assert sold.actual_commissions == Decimal("4")
    assert sold.net_pnl == Decimal("346")


def test_full_exit_keeps_realized_result_and_zeroes_open_position_values() -> None:
    service = PositionCalculationService()
    opened = service.apply_execution(
        None,
        side=OrderSide.BUY,
        executed_lots=2,
        lot_size=1,
        price=Decimal("50"),
        commission=Decimal("0.5"),
        current_price=Decimal("50"),
    )

    closed = service.apply_execution(
        opened,
        side=OrderSide.SELL,
        executed_lots=2,
        lot_size=1,
        price=Decimal("60"),
        commission=Decimal("0.5"),
        current_price=Decimal("60"),
    )

    assert closed.quantity_units == 0
    assert closed.average_price == Decimal("0")
    assert closed.invested_amount == Decimal("0")
    assert closed.realized_pnl == Decimal("20")
    assert closed.net_pnl == Decimal("19")
