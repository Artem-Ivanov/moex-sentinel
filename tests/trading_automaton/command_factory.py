"""Shared synthetic baseline command factory for Worker tests."""

from decimal import Decimal
from uuid import UUID, uuid5

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand, BrokerPositionBootstrap
from trading_automaton.storage.repository import DecisionBatchItem, IntentBatchItem

_NAMESPACE = UUID("00000000-0000-4000-8000-000000000001")


def command(
    *,
    automation: str = "automation",
    broker: str = "broker",
    account: str = "account",
    instrument: str = "instrument",
    currency: str = "RUB",
    lot_size: int = 10,
    state: AutomationState = AutomationState.IN_WORK,
    revision: int = 1,
    last_sequence_number: int = 0,
    bootstrap: BrokerPositionBootstrap | None = None,
) -> AutomationCommand:
    return AutomationCommand(
        automation_id=uuid5(_NAMESPACE, f"automation:{automation}"),
        user_broker_id=uuid5(_NAMESPACE, f"user-broker:{broker}:{account}"),
        broker_id=uuid5(_NAMESPACE, f"broker:{broker}"),
        account_id=account,
        external_instrument_id=instrument,
        instrument_id=uuid5(_NAMESPACE, f"instrument:{broker}:{instrument}"),
        currency=currency,
        lot_size=lot_size,
        min_price_increment=Decimal("0.01"),
        state=state,
        revision=revision,
        last_sequence_number=last_sequence_number,
        resume_requested=False,
        bootstrap=bootstrap,
    )


def decision_item(
    command_value: AutomationCommand,
    *,
    intent_id: str = "00000000-0000-4000-8000-000000000405",
    process_id: str = "00000000-0000-4000-8000-000000000406",
) -> DecisionBatchItem:
    return DecisionBatchItem(
        automation_id=str(command_value.automation_id),
        broker_id=str(command_value.broker_id),
        account_id=command_value.account_id,
        instrument_id=command_value.external_instrument_id,
        instrument_type="SHARE",
        quantity_lots=1,
        lot_size=command_value.lot_size,
        average_price=Decimal("100"),
        current_price=Decimal("100"),
        best_bid=Decimal("99.9"),
        best_ask=Decimal("100"),
        invested_amount=Decimal("1000"),
        estimated_commission=Decimal("1"),
        decision="BUY_MORE",
        reason_code="TEST",
        decision_quantity_lots=1,
        limit_price=Decimal("100"),
        strategy_snapshot={},
        process_id=process_id,
        intent=IntentBatchItem(intent_id, "BUY_MORE", "BUY", 1, Decimal("100")),
        currency=command_value.currency,
    )
