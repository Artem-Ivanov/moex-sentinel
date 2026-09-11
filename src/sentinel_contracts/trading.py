"""Trading automation contracts shared across process boundaries."""

from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class AutomationState(str, Enum):
    IN_QUEUE = "IN_QUEUE"
    OPENING = "OPENING"
    IN_WORK = "IN_WORK"
    HOLD = "HOLD"
    CLOSED = "CLOSED"


class DecisionKind(str, Enum):
    BUY_MORE = "BUY_MORE"
    WAIT = "WAIT"
    SELL_PART = "SELL_PART"
    SELL_ALL = "SELL_ALL"
    NO_ACTION = "NO_ACTION"


class PositionSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    quantity_lots: int
    average_price: Decimal
    invested_amount: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    actual_commissions: Decimal

    def to_metadata(self) -> dict[str, int | str]:
        return {
            "quantity_lots": self.quantity_lots,
            "average_price": str(self.average_price),
            "invested_amount": str(self.invested_amount),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "net_pnl": str(self.net_pnl),
            "actual_commissions": str(self.actual_commissions),
        }

    @classmethod
    def from_metadata(cls, value: object) -> "PositionSnapshot":
        if not isinstance(value, dict):
            raise TypeError("Position snapshot must be an object.")
        try:
            quantity_lots = int(value["quantity_lots"])
            decimals = [
                Decimal(str(value[name]))
                for name in (
                    "average_price",
                    "invested_amount",
                    "realized_pnl",
                    "unrealized_pnl",
                    "net_pnl",
                    "actual_commissions",
                )
            ]
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise ValueError("Position snapshot is invalid.") from error
        if quantity_lots < 0:
            raise ValueError("Position quantity cannot be negative.")
        return cls(
            quantity_lots=quantity_lots,
            average_price=decimals[0],
            invested_amount=decimals[1],
            realized_pnl=decimals[2],
            unrealized_pnl=decimals[3],
            net_pnl=decimals[4],
            actual_commissions=decimals[5],
        )
