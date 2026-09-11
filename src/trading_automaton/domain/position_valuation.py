"""Pure valuation shared by decision snapshots and terminal execution snapshots."""

from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.trading import PositionSnapshot


class CommissionLot(Protocol):
    @property
    def entry_commission(self) -> Decimal: ...

    @property
    def original_lots(self) -> int: ...

    @property
    def remaining_lots(self) -> int: ...


class ValuationLot(CommissionLot, Protocol):
    @property
    def entry_price(self) -> Decimal: ...


def net_position_pnl(realized: Decimal, unrealized: Decimal, lots: Iterable[CommissionLot]) -> Decimal:
    """Combine net realized and gross unrealized, deducting remaining paid entry fees.

    Closed allocations already include their entry and exit fees in realized P&L.
    Future exit commissions are not actual costs and are not included here.
    """
    open_entry_fees = sum(
        (lot.entry_commission * lot.remaining_lots / lot.original_lots for lot in lots if lot.remaining_lots > 0),
        start=Decimal(),
    )
    return realized + unrealized - open_entry_fees


def lot_position_snapshot(
    lots: Iterable[ValuationLot],
    *,
    lot_size: int,
    mark_price: Decimal,
    realized_pnl: Decimal,
    actual_commissions: Decimal,
) -> PositionSnapshot:
    """Value the same remaining LIFO lots that produced the realized result."""
    remaining = tuple(lot for lot in lots if lot.remaining_lots > 0)
    quantity = sum(lot.remaining_lots for lot in remaining)
    cost_per_lot = sum((lot.entry_price * lot.remaining_lots for lot in remaining), start=Decimal())
    average = cost_per_lot / quantity if quantity else Decimal()
    invested = cost_per_lot * lot_size
    unrealized = mark_price * lot_size * quantity - invested
    return PositionSnapshot(
        quantity_lots=quantity,
        average_price=average,
        invested_amount=invested,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized,
        net_pnl=net_position_pnl(realized_pnl, unrealized, remaining),
        actual_commissions=actual_commissions,
    )
