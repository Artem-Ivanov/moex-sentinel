"""Worker storage scenario inputs shared by durability and execution tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.trading_automaton.command_factory import command
from trading_automaton.storage.repository import ExecutionFinalization

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)

INTENT_ID = "00000000-0000-4000-8000-000000000405"

AUDIT_ID = "00000000-0000-4000-8000-000000000407"

AUDIT_PROCESS_ID = "00000000-0000-4000-8000-000000000408"

SELL_INTENT_ID = "00000000-0000-4000-8000-000000000410"


def baseline_command():
    return command(broker="broker-1", account="account-1", instrument="instrument-1")


def filled_buy() -> ExecutionFinalization:
    command_value = baseline_command()
    return ExecutionFinalization(
        intent_id=INTENT_ID,
        automation_id=str(command_value.automation_id),
        broker_id=str(command_value.broker_id),
        account_id=command_value.account_id,
        instrument_id=command_value.external_instrument_id,
        side="BUY",
        quantity_lots=1,
        requested_price=Decimal("100"),
        currency="RUB",
        state="FILLED",
        occurred_at=NOW + timedelta(milliseconds=10),
        broker_order_id="synthetic-order",
        requested_amount=Decimal("1000"),
        executed_amount=Decimal("1000"),
        estimated_commission=Decimal("1"),
        executed_commission=Decimal("2"),
        executed_lots=1,
        executed_price=Decimal("100"),
        executed_at=NOW + timedelta(milliseconds=10),
        terminal_at=NOW + timedelta(milliseconds=10),
        lot_size=10,
        process_id="00000000-0000-4000-8000-000000000406",
    )
