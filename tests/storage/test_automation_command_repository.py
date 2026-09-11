"""Native Worker command projection from baseline Core tables."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from moex_sentinel.storage.models import Base, TradingAutomationModel
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from sentinel_contracts.trading import AutomationState
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

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
    repo, _factory = repository()

    command = repo.claim(10)[0]

    assert command.automation_id == UUID(AUTOMATION_ID)
    assert command.user_broker_id == UUID(SCOPE_ID)
    assert command.external_instrument_id == f"external-{INSTRUMENT_ID}"
    assert command.instrument_id == UUID(INSTRUMENT_ID)
    assert command.currency == "RUB"
    assert command.account_id == "synthetic-account"
    assert not hasattr(command, "strategy")


def test_statuses_preserve_request_order_and_report_missing_ids() -> None:
    repo, _factory = repository()
    missing = UUID("00000000-0000-4000-8000-000000000199")

    result = repo.statuses([missing, UUID(AUTOMATION_ID)])

    assert [item.automation_id for item in result.automations] == [UUID(AUTOMATION_ID)]
    assert result.missing_automation_ids == (missing,)


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


def test_hold_command_contains_immutable_broker_position_snapshot() -> None:
    repo, factory = repository()
    cycle_id = "00000000-0000-4000-8000-000000000150"
    lot_id = "00000000-0000-4000-8000-000000000151"
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, AUTOMATION_ID)
        automation.state = AutomationState.HOLD.value
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
