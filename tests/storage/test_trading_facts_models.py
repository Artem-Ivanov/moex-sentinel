"""Baseline trading fact model tests."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import (
    AutomationEventModel,
    Base,
    BrokerAccountFeeProfileModel,
    BrokerOrderEventModel,
    BrokerOrderModel,
    ExecutionLotAllocationModel,
    PositionCycleModel,
    PositionLotModel,
    PositionValuationSnapshotModel,
    TradeAuditEventModel,
    TradeDecisionModel,
    TradeExecutionModel,
    TradingAutomationModel,
)
from tests.storage.trading_facts_helpers import NOW, instrument_model, user_broker_model


@pytest.fixture
def database() -> Iterator[tuple[Engine, Session]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield engine, session
    engine.dispose()


def seed_two_scopes(session: Session) -> None:
    session.add_all(
        [
            user_broker_model("scope-1", "account-1"),
            user_broker_model("scope-2", "account-2"),
        ]
    )
    session.flush()
    session.add_all(
        [
            instrument_model("instrument-1", "scope-1"),
            instrument_model("instrument-2", "scope-2"),
        ]
    )
    session.flush()


def automation_model(
    record_id: str,
    *,
    user_broker_id: str = "scope-1",
    instrument_id: str = "instrument-1",
    state: str = "IN_WORK",
    closed: bool = False,
) -> TradingAutomationModel:
    return TradingAutomationModel(
        id=record_id,
        user_broker_id=user_broker_id,
        instrument_id=instrument_id,
        state=state,
        suspended_from_state=None,
        hold_reason=None,
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
        closed_at=NOW if closed else None,
        created_at=NOW,
        updated_at=NOW,
    )


def position_cycle_model(
    record_id: str,
    *,
    state: str = "OPEN",
    closed: bool = False,
) -> PositionCycleModel:
    return PositionCycleModel(
        id=record_id,
        user_broker_id="scope-1",
        automation_id="automation-1",
        instrument_id="instrument-1",
        state=state,
        quantity_lots=1,
        average_entry_price=Decimal("10"),
        invested_amount=Decimal("10"),
        realized_pnl=Decimal(),
        unrealized_pnl=Decimal(),
        net_pnl=Decimal(),
        accumulated_commissions=Decimal("0.01"),
        opened_at=NOW,
        closed_at=NOW if closed else None,
        created_at=NOW,
        updated_at=NOW,
    )


def seed_automation(session: Session) -> None:
    seed_two_scopes(session)
    session.add(automation_model("automation-1"))
    session.flush()


def seed_cycle(session: Session) -> None:
    seed_automation(session)
    session.add(position_cycle_model("cycle-1"))
    session.flush()


def decision_model(
    record_id: str = "decision-1",
    *,
    user_broker_id: str = "scope-1",
    automation_id: str = "automation-1",
    instrument_id: str = "instrument-1",
    fact_id: str = "fact-decision-1",
) -> TradeDecisionModel:
    return TradeDecisionModel(
        id=record_id,
        fact_id=fact_id,
        process_id="process-1",
        user_broker_id=user_broker_id,
        automation_id=automation_id,
        position_cycle_id="cycle-1",
        instrument_id=instrument_id,
        quantity_lots=1,
        lot_size=10,
        average_price=Decimal("10"),
        invested_amount=Decimal("10"),
        current_price=Decimal("10.25"),
        best_bid=Decimal("10.24"),
        best_ask=Decimal("10.26"),
        indicators={"signal": "synthetic"},
        estimated_commission=Decimal("0.01"),
        decision="BUY_MORE",
        reason_code="SYNTHETIC_SIGNAL",
        requested_quantity_lots=1,
        limit_price=Decimal("10.25"),
        strategy_snapshot={"version": 1},
        decided_at=NOW,
        created_at=NOW,
    )


def order_model(
    record_id: str,
    *,
    decision_id: str = "decision-1",
    user_broker_id: str = "scope-1",
    automation_id: str = "automation-1",
    instrument_id: str = "instrument-1",
    fact_id: str | None = None,
    idempotency_key: str | None = None,
) -> BrokerOrderModel:
    return BrokerOrderModel(
        id=record_id,
        fact_id=fact_id or f"fact-{record_id}",
        user_broker_id=user_broker_id,
        automation_id=automation_id,
        decision_id=decision_id,
        position_cycle_id="cycle-1",
        instrument_id=instrument_id,
        idempotency_key=idempotency_key or f"idempotency-{record_id}",
        external_order_id=None,
        intent_kind="BUY_MORE",
        side="BUY",
        order_type="LIMIT",
        state="CREATED",
        quantity_lots=1,
        limit_price=Decimal("10.25"),
        requested_amount=Decimal("10.25"),
        executed_amount=Decimal(),
        estimated_commission=Decimal("0.01"),
        executed_commission=Decimal(),
        strategy_snapshot={"version": 1},
        dispatch_started_at=None,
        broker_responded_at=None,
        executed_at=None,
        terminal_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def execution_model(
    record_id: str,
    *,
    external_execution_id: str | None,
    source: str,
) -> TradeExecutionModel:
    return TradeExecutionModel(
        id=record_id,
        fact_id=f"fact-{record_id}",
        user_broker_id="scope-1",
        automation_id="automation-1",
        broker_order_id="order-1",
        position_cycle_id="cycle-1",
        instrument_id="instrument-1",
        external_execution_id=external_execution_id,
        side="BUY",
        executed_lots=1,
        price=Decimal("10.25"),
        value=Decimal("10.25"),
        broker_commission=Decimal("0.01"),
        other_fees=Decimal(),
        currency="RUB",
        source=source,
        executed_at=NOW,
        created_at=NOW,
    )


def seed_order(session: Session) -> None:
    seed_cycle(session)
    session.add(decision_model())
    session.flush()
    session.add(order_model("order-1"))
    session.flush()


def seed_buy_and_sell_executions(session: Session) -> None:
    seed_order(session)
    buy = execution_model("execution-buy", external_execution_id="external-buy", source="BROKER_FILL")
    sell = execution_model("execution-sell", external_execution_id="external-sell", source="BROKER_FILL")
    sell.side = "SELL"
    session.add_all([buy, sell])
    session.flush()


def position_lot_model(record_id: str = "lot-1", *, remaining_lots: int = 1) -> PositionLotModel:
    return PositionLotModel(
        id=record_id,
        user_broker_id="scope-1",
        automation_id="automation-1",
        position_cycle_id="cycle-1",
        buy_execution_id="execution-buy",
        source="BROKER_EXECUTION",
        original_lots=1,
        remaining_lots=remaining_lots,
        entry_price=Decimal("10.25"),
        entry_commission=Decimal("0.01"),
        opened_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def allocation_model(record_id: str) -> ExecutionLotAllocationModel:
    return ExecutionLotAllocationModel(
        id=record_id,
        user_broker_id="scope-1",
        automation_id="automation-1",
        position_cycle_id="cycle-1",
        sell_execution_id="execution-sell",
        position_lot_id="lot-1",
        allocated_lots=1,
        entry_value=Decimal("10.25"),
        exit_value=Decimal("11"),
        entry_commission=Decimal("0.01"),
        exit_commission=Decimal("0.01"),
        realized_pnl=Decimal("0.73"),
        allocated_at=NOW,
        created_at=NOW,
    )


def test_automation_cannot_reference_instrument_from_another_scope(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_two_scopes(session)
    session.add(
        automation_model(
            "automation-1",
            user_broker_id="scope-2",
            instrument_id="instrument-1",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_only_one_non_closed_automation_exists_per_scoped_instrument(
    database: tuple[Engine, Session],
) -> None:
    _, session = database
    seed_two_scopes(session)
    session.add_all(
        [
            automation_model("automation-1", state="IN_WORK"),
            automation_model("automation-2", state="HOLD"),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_closed_automation_does_not_block_new_active_automation(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_two_scopes(session)
    session.add_all(
        [
            automation_model("automation-1", state="CLOSED", closed=True),
            automation_model("automation-2", state="IN_QUEUE"),
        ]
    )

    session.flush()


def test_only_one_open_cycle_exists_per_scoped_automation(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_automation(session)
    session.add_all([position_cycle_model("cycle-1"), position_cycle_model("cycle-2")])

    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    ("cycle_state", "closed"),
    [("OPEN", True), ("CLOSED", False)],
)
def test_cycle_state_and_closed_timestamp_are_consistent(
    database: tuple[Engine, Session],
    cycle_state: str,
    closed: bool,
) -> None:
    _, session = database
    seed_automation(session)
    session.add(position_cycle_model("cycle-1", state=cycle_state, closed=closed))

    with pytest.raises(IntegrityError):
        session.flush()


def test_revisions_sequences_and_quantities_reject_negative_values(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_two_scopes(session)
    invalid = automation_model("automation-1")
    invalid.revision = 0
    invalid.last_sequence_number = -1
    session.add(invalid)

    with pytest.raises(IntegrityError):
        session.flush()


def test_decision_creates_at_most_one_order(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_cycle(session)
    session.add(decision_model())
    session.flush()
    session.add_all([order_model("order-1"), order_model("order-2")])

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_non_null_external_execution_id_is_rejected(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_order(session)
    session.add_all(
        [
            execution_model("execution-1", external_execution_id="external-1", source="BROKER_FILL"),
            execution_model("execution-2", external_execution_id="external-1", source="BROKER_FILL"),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_broker_fill_requires_external_execution_id(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_order(session)
    session.add(execution_model("execution-1", external_execution_id=None, source="BROKER_FILL"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_order_cannot_cross_user_broker_scope(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_cycle(session)
    session.add(decision_model())
    session.flush()
    session.add(
        order_model(
            "order-1",
            user_broker_id="scope-2",
            automation_id="automation-1",
            instrument_id="instrument-2",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_fact_identity_is_unique_for_decisions(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_cycle(session)
    session.add_all(
        [
            decision_model("decision-1", fact_id="same-fact"),
            decision_model("decision-2", fact_id="same-fact"),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_order_event_cannot_cross_order_scope(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_order(session)
    session.add(
        BrokerOrderEventModel(
            id="event-1",
            fact_id="fact-event-1",
            user_broker_id="scope-2",
            automation_id="automation-1",
            broker_order_id="order-1",
            from_state=None,
            to_state="CREATED",
            safe_reason="CREATED",
            safe_message="Synthetic event",
            occurred_at=NOW,
            created_at=NOW,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_sell_execution_allocates_source_lot_once(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_buy_and_sell_executions(session)
    session.add(position_lot_model())
    session.flush()
    session.add_all([allocation_model("allocation-1"), allocation_model("allocation-2")])

    with pytest.raises(IntegrityError):
        session.flush()


def test_remaining_lots_cannot_exceed_original_lots(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_buy_and_sell_executions(session)
    session.add(position_lot_model(remaining_lots=2))

    with pytest.raises(IntegrityError):
        session.flush()


def test_automation_envelope_sequence_is_unique(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_automation(session)
    session.add_all(
        [
            AutomationEventModel(
                event_id="event-1",
                automation_id="automation-1",
                user_broker_id="scope-1",
                sequence_number=1,
                expected_revision=1,
                fact_kind="TRADE_DECISION",
                safe_message="Synthetic event one",
                payload={},
                occurred_at=NOW,
                received_at=NOW,
            ),
            AutomationEventModel(
                event_id="event-2",
                automation_id="automation-1",
                user_broker_id="scope-1",
                sequence_number=1,
                expected_revision=1,
                fact_kind="BROKER_ORDER",
                safe_message="Synthetic event two",
                payload={},
                occurred_at=NOW,
                received_at=NOW,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_audit_requires_automation(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_two_scopes(session)
    session.add(
        TradeAuditEventModel(
            event_id="audit-1",
            process_id="process-1",
            parent_process_id=None,
            user_broker_id="scope-1",
            automation_id="missing",
            decision_id=None,
            broker_order_id=None,
            execution_id=None,
            instrument_id="instrument-1",
            level="INFO",
            stage="SYNTHETIC",
            safe_message="Synthetic audit",
            data={},
            occurred_at=NOW,
            created_at=NOW,
            critical=False,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_fee_profile_is_unique_by_scoped_market(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_two_scopes(session)
    profiles = [
        BrokerAccountFeeProfileModel(
            id=f"profile-{number}",
            user_broker_id="scope-1",
            instrument_type="SHARE",
            currency="RUB",
            buy_rate=Decimal("0.0005"),
            sell_rate=Decimal("0.0005"),
            service_rate=Decimal(),
            deal_rate=Decimal(),
            source="SYNTHETIC",
            calculated_at=NOW,
            valid_until=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        for number in (1, 2)
    ]
    session.add_all(profiles)

    with pytest.raises(IntegrityError):
        session.flush()


def test_position_valuation_snapshots_accept_capture_rows(database: tuple[Engine, Session]) -> None:
    _, session = database
    seed_automation(session)
    session.add(position_cycle_model("cycle-1"))
    session.flush()
    session.add(
        PositionValuationSnapshotModel(
            id="valuation-1",
            user_broker_id="scope-1",
            automation_id="automation-1",
            position_cycle_id="cycle-1",
            instrument_id="instrument-1",
            quantity_lots=1,
            average_price=Decimal("10"),
            current_price=Decimal("9"),
            invested_amount=Decimal("10"),
            market_value=Decimal("9"),
            realized_pnl=Decimal("-1"),
            unrealized_pnl=Decimal("-1"),
            net_pnl=Decimal("-2"),
            actual_commissions=Decimal("0.01"),
            source="SYNTHETIC",
            captured_at=NOW,
            created_at=NOW,
        )
    )

    session.flush()
