"""Static contract for DTOs that intentionally preserve positional construction."""

from decimal import Decimal

from moex_sentinel.domain.portfolio import Money
from trading_automaton.domain.dtos import PositionWorkItem, PreparedDecision

money = Money(Decimal("10.50"), "RUB")

amount: Decimal = money.amount
currency: str = money.currency


def automation_ids(item: PositionWorkItem, prepared: PreparedDecision) -> tuple[str, str]:
    """The scheduling DTOs must retain the concrete automation command type."""

    return item.command.automation_id, prepared.command.automation_id
