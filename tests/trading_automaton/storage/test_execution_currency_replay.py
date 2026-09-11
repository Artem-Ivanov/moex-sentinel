from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.trading_automaton.command_factory import command, decision_item
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import ExecutionFinalization, LocalAutomationRepository

BASELINE_INTENT_ID = "00000000-0000-4000-8000-000000000405"


def finalization(currency: str) -> ExecutionFinalization:
    command_value = command(broker="broker-1", account="account-1", instrument="instrument-1")
    occurred_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
    return ExecutionFinalization(
        intent_id=BASELINE_INTENT_ID,
        automation_id=str(command_value.automation_id),
        broker_id=str(command_value.broker_id),
        account_id="account-1",
        instrument_id="instrument-1",
        side="BUY",
        quantity_lots=1,
        requested_price=Decimal("100"),
        currency=currency,
        state="FILLED",
        occurred_at=occurred_at,
        broker_order_id="order-1",
        requested_amount=Decimal("1000"),
        executed_amount=Decimal("1000"),
        estimated_commission=Decimal("1"),
        executed_commission=Decimal("2"),
        executed_lots=1,
        executed_price=Decimal("100"),
        executed_at=occurred_at,
        terminal_at=occurred_at,
        lot_size=10,
        process_id="process-1",
    )


def test_currency_replay_remains_durable_after_execution_outbox_acknowledgement(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    command_value = command(broker="broker-1", account="account-1", instrument="instrument-1")
    automation_id = str(command_value.automation_id)
    repository.cache_command(command_value)
    occurred_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
    repository.save_decision_batch((decision_item(command_value),), occurred_at=occurred_at)
    applied = repository.finalize_execution(finalization("RUB"))
    repository.acknowledge_fact_outbox(
        automation_id,
        accepted_through_sequence=100,
        current_revision=2,
    )

    replay = repository.finalize_execution(finalization("RUB"))

    assert applied.applied is True
    assert replay.applied is False
    with pytest.raises(ValueError, match="currency conflicts"):
        repository.finalize_execution(finalization("USD"))
