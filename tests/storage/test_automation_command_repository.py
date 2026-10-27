"""Native Worker command projection from baseline Core tables."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from moex_sentinel.storage.models import Base, TradingAutomationModel
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from sentinel_contracts.trading import AutomationState
from tests.storage.trading_facts_helpers import (
    NOW,
    decision_model,
    instrument_model,
    order_model,
    position_cycle_model,
    user_broker_model,
)

SCOPE_ID = "00000000-0000-4000-8000-000000000101"
INSTRUMENT_ID = "00000000-0000-4000-8000-000000000102"
AUTOMATION_ID = "00000000-0000-4000-8000-000000000103"
NOW = datetime(2026, 8, 14, 10, tzinfo=UTC)


def repository() -> tuple[AutomationCommandRepository, sessionmaker]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(user_broker_model(SCOPE_ID, "synthetic-account"))
        session.add(instrument_model(INSTRUMENT_ID, SCOPE_ID))
        session.add(
            TradingAutomationModel(
                id=AUTOMATION_ID,
                user_broker_id=SCOPE_ID,
                instrument_id=INSTRUMENT_ID,
                state=AutomationState.IN_QUEUE.value,
                suspended_from_state=None,
                hold_reason=None,
                revision=1,
                last_sequence_number=0,
                resume_requested=False,
                closed_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return AutomationCommandRepository(factory), factory


def test_claim_reads_strategy_free_command_directly_from_baseline_tables() -> None:
    repo, factory = repository()
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.settings_revision = 3
        automation.order_quota_percent = Decimal("12.5")

    command = repo.claim(10)[0]

    assert command.automation_id == UUID(AUTOMATION_ID)
    assert command.user_broker_id == UUID(SCOPE_ID)
    assert command.external_instrument_id == f"external-{INSTRUMENT_ID}"
    assert command.instrument_id == UUID(INSTRUMENT_ID)
    assert command.currency == "RUB"
    assert command.account_id == "synthetic-account"
    assert command.settings_revision == 3
    assert command.order_quota_percent == Decimal("12.5")
    assert not hasattr(command, "strategy")


def test_statuses_preserve_request_order_and_report_missing_ids() -> None:
    repo, factory = repository()
    missing = UUID("00000000-0000-4000-8000-000000000199")
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.settings_revision = 2
        automation.order_quota_percent = Decimal("10")

    result = repo.statuses([missing, UUID(AUTOMATION_ID)])

    assert [item.automation_id for item in result.automations] == [UUID(AUTOMATION_ID)]
    assert result.missing_automation_ids == (missing,)
    assert result.automations[0].settings_revision == 2
    assert result.automations[0].order_quota_percent == Decimal("10")


def _seed_fee_order(factory, *, current_status: str | None, current_amount: str | None = None) -> UUID:
    intent_id = UUID("00000000-0000-4000-8000-000000000160")
    previous_pass = "00000000-0000-4000-8000-000000000161"
    current_pass = "00000000-0000-4000-8000-000000000162"
    with factory.begin() as session:
        broker = session.get_one(UserBrokerModel, SCOPE_ID)
        broker.environment = "PROD"
        cycle = position_cycle_model("cycle-1")
        cycle.user_broker_id = SCOPE_ID
        cycle.automation_id = AUTOMATION_ID
        cycle.instrument_id = INSTRUMENT_ID
        session.add(cycle)
        decision = decision_model()
        decision.user_broker_id = SCOPE_ID
        decision.automation_id = AUTOMATION_ID
        decision.position_cycle_id = "cycle-1"
        decision.instrument_id = INSTRUMENT_ID
        session.add(decision)
        order = order_model(
            "fee-order",
            decision_id="decision-1",
            user_broker_id=SCOPE_ID,
            automation_id=AUTOMATION_ID,
            instrument_id=INSTRUMENT_ID,
            idempotency_key=str(intent_id),
        )
        order.position_cycle_id = "cycle-1"
        order.external_order_id = "exchange-parent"
        order.state = "EXPIRED"
        order.executed_amount = Decimal("10.25")
        order.executed_commission = Decimal("0.14")
        order.terminal_at = NOW
        session.add(order)
        session.add(
            BrokerFeeScanStateModel(
                user_broker_id=SCOPE_ID,
                scan_pass_id=current_pass,
                completed_scan_pass_id=current_pass,
                lower_bound=NOW,
                upper_bound=NOW,
                seen_cursors=[],
                status="COMPLETE",
                completed_at=NOW,
            )
        )
        session.add(
            BrokerOrderFeeObservationModel(
                user_broker_id=SCOPE_ID,
                broker_order_id="fee-order",
                parent_operation_id="exchange-parent",
                operation_type="BROKER_FEE",
                currency="RUB",
                scan_pass_id=previous_pass,
                operation_id="old-known",
                amount=Decimal("3.58"),
                status="KNOWN",
                occurred_at=NOW,
                observed_at=NOW,
            )
        )
        if current_status is not None:
            session.add(
                BrokerOrderFeeObservationModel(
                    user_broker_id=SCOPE_ID,
                    broker_order_id="fee-order",
                    parent_operation_id="exchange-parent",
                    operation_type="BROKER_FEE",
                    currency="RUB",
                    scan_pass_id=current_pass,
                    operation_id="current-fee",
                    amount=Decimal(current_amount or "1.00"),
                    status=current_status,
                    occurred_at=NOW,
                    observed_at=NOW,
                )
            )
    return intent_id


@pytest.mark.parametrize(
    ("current_status", "current_amount", "expected_status"),
    [(None, None, "MISSING"), ("UNKNOWN", "1.00", "UNKNOWN"), ("KNOWN", "3.75", "KNOWN")],
)
def test_commission_status_retains_last_trusted_absolute_amount(current_status, current_amount, expected_status):
    repo, factory = repository()
    intent_id = _seed_fee_order(factory, current_status=current_status, current_amount=current_amount)

    result = repo.statuses([UUID(AUTOMATION_ID)], commission_order_ids=[intent_id])

    assert len(result.commission_statuses) == 1
    status = result.commission_statuses[0]
    assert status.commission_status == expected_status
    expected_amount = Decimal("3.75") if expected_status == "KNOWN" else Decimal("3.58")
    assert status.effective_broker_commission == expected_amount


def test_fee_status_is_not_returned_for_unrequested_automation_or_empty_automation_batch() -> None:
    repo, factory = repository()
    intent_id = _seed_fee_order(factory, current_status="KNOWN", current_amount="3.58")

    assert repo.statuses([], commission_order_ids=[intent_id]).commission_statuses == ()
    assert repo.statuses(
        [UUID("00000000-0000-4000-8000-000000000199")], commission_order_ids=[intent_id]
    ).commission_statuses == ()


def test_hold_automation_is_claimable_for_bootstrap_or_resume() -> None:
    repo, factory = repository()
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.state = AutomationState.HOLD.value
        automation.hold_reason = "BOOTSTRAPPING"
        automation.resume_requested = True

    assert repo.claim(10)[0].state is AutomationState.HOLD


def test_runtime_hold_without_resume_is_not_claimed() -> None:
    repo, factory = repository()
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.state = AutomationState.HOLD.value
        automation.hold_reason = "RUNTIME_GUARD"
        automation.revision = 2
        automation.last_sequence_number = 10

    assert repo.claim(10) == []


@pytest.mark.parametrize("state", [AutomationState.HOLD, AutomationState.IN_QUEUE])
def test_unfinished_bootstrap_command_contains_immutable_broker_position_snapshot(state: AutomationState) -> None:
    repo, factory = repository()
    cycle_id = "00000000-0000-4000-8000-000000000150"
    lot_id = "00000000-0000-4000-8000-000000000151"
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.state = state.value
        automation.hold_reason = "BOOTSTRAPPING"
        automation.bootstrap_position_cycle_id = cycle_id
        automation.bootstrap_position_lot_id = lot_id
        automation.bootstrap_quantity_lots = 2
        automation.bootstrap_average_price = Decimal("100")
        automation.bootstrap_invested_amount = Decimal("2000")
        automation.bootstrap_currency = "RUB"
        automation.bootstrap_observed_at = NOW

    command = repo.claim(10)[0]

    assert command.bootstrap is not None
    assert command.bootstrap.position_cycle_id == UUID(cycle_id)
    assert command.bootstrap.position_lot_id == UUID(lot_id)
    assert command.bootstrap.quantity_lots == 2
    assert command.bootstrap.average_price == Decimal("100")


def test_completed_bootstrap_snapshot_is_not_reapplied_on_resume() -> None:
    repo, factory = repository()
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.revision = 2
        automation.last_sequence_number = 4
        automation.bootstrap_position_cycle_id = "00000000-0000-4000-8000-000000000150"
        automation.bootstrap_position_lot_id = "00000000-0000-4000-8000-000000000151"
        automation.bootstrap_quantity_lots = 2
        automation.bootstrap_average_price = Decimal("100")
        automation.bootstrap_invested_amount = Decimal("2000")
        automation.bootstrap_currency = "RUB"
        automation.bootstrap_observed_at = NOW

    command = repo.claim(10)[0]

    assert command.state is AutomationState.IN_QUEUE
    assert command.bootstrap is None
