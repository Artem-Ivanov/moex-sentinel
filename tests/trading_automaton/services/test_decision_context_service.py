"""Build deterministic strategy input from broker facts and intent history."""

from datetime import UTC, datetime
from decimal import Decimal

from sentinel_contracts.broker_execution import (
    BrokerPosition,
    OrderBookLevel,
    OrderBookSnapshot,
)
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.storage.repository import IntentHistory


def test_builds_conservative_context_from_broker_facts() -> None:
    command = baseline_command(
        automation="automation-1",
        broker="broker-1",
        account="account-1",
        instrument="instrument-1",
        revision=2,
        last_sequence_number=1,
    )
    position = BrokerPosition("instrument-1", Decimal("4"), Decimal("100"), Decimal("99"), "RUB")
    book = OrderBookSnapshot(
        bids=(OrderBookLevel(Decimal("98.9"), 10),),
        asks=(OrderBookLevel(Decimal("99.1"), 10),),
        captured_at=datetime(2026, 8, 6, tzinfo=UTC),
    )
    history = IntentHistory(Decimal("98"), 2, Decimal("6"), 4, Decimal("2"))

    settings = StrategySettings(STRATEGY_BUY_ORDER_LOTS=2)
    context = DecisionContextService(settings).build(
        command,
        position,
        book,
        history,
        commission_schedule=CommissionSchedule(Decimal("0.001"), Decimal("0.002")),
        averaging_step_percent=Decimal("0.1"),
        minimum_net_profit_percent=Decimal("0.05"),
        free_cash=Decimal("5000"),
        reserved_cash=Decimal("1000"),
    )

    assert context.quantity_lots == 4
    assert context.current_price == Decimal("98.9")
    assert context.invested_amount == Decimal("4000")
    assert context.last_buy_price == Decimal("98")
    assert context.completed_partial_sell_steps == 2
    assert context.commission_schedule == CommissionSchedule(Decimal("0.001"), Decimal("0.002"))
    assert context.buy_commission_per_lot == Decimal("0.5")
    assert context.averaging_step_percent == Decimal("0.1")
    assert context.minimum_net_profit_percent == Decimal("0.05")
    assert context.free_cash == Decimal("5000")
    assert context.reserved_cash == Decimal("1000")
    assert context.available_free_cash == Decimal("4000")
    assert context.settings is settings
    assert context.currency == "RUB"
    assert context.required_order_cash == Decimal("1983.982")
