"""Apply an automation's nominal order ceiling to discretionary orders."""

from decimal import Decimal
from fractions import Fraction

from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import TradeDecision


def quota_lots(
    available_free_cash: Decimal,
    percent: Decimal,
    positive_position_count: int | None,
    *,
    price: Decimal,
    lot_size: int,
) -> int:
    if positive_position_count is None or positive_position_count < 0:
        return 0
    if (
        not available_free_cash.is_finite()
        or not percent.is_finite()
        or not price.is_finite()
        or price <= 0
        or lot_size <= 0
    ):
        return 0
    cash = max(Decimal(), available_free_cash)
    quota_lots = (
        Fraction(cash) * Fraction(percent) / (100 * max(1, positive_position_count) * Fraction(price) * lot_size)
    )
    return quota_lots.numerator // quota_lots.denominator


def apply_order_quota(
    decision: TradeDecision,
    command: AutomationCommand,
    *,
    available_free_cash: Decimal,
    positive_position_count: int | None,
    best_bid: Decimal,
    best_ask: Decimal,
) -> TradeDecision:
    percent = command.order_quota_percent
    if percent is None or decision.kind not in {DecisionKind.BUY_MORE, DecisionKind.SELL_PART}:
        return decision
    if positive_position_count is None or positive_position_count < 0:
        return TradeDecision(DecisionKind.WAIT, 0, None, "POSITION_COUNT_UNAVAILABLE")
    price = best_ask if decision.kind is DecisionKind.BUY_MORE else best_bid
    if not price.is_finite() or price <= 0:
        return TradeDecision(DecisionKind.WAIT, 0, None, "ORDER_QUOTA_PRICE_UNAVAILABLE")
    allowed_lots = quota_lots(
        available_free_cash,
        percent,
        positive_position_count,
        price=price,
        lot_size=command.lot_size,
    )
    lots = allowed_lots if decision.kind is DecisionKind.BUY_MORE else min(decision.quantity_lots, allowed_lots)
    if lots <= 0:
        return TradeDecision(DecisionKind.WAIT, 0, None, "ORDER_QUOTA_BELOW_ONE_LOT")
    return decision.model_copy(update={"quantity_lots": lots})
