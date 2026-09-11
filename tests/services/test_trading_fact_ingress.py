"""Per-automation transactional acceptance of typed trading facts."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypeVar
from uuid import UUID

import pytest
from pydantic import TypeAdapter
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import BrokerOrderStatus
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import (
    AutomationEventModel,
    Base,
    BrokerOrderEventModel,
    BrokerOrderModel,
    ExecutionLotAllocationModel,
    PositionCycleModel,
    PositionLotModel,
    TradeAuditEventModel,
    TradeDecisionModel,
    TradeExecutionModel,
    TradingAutomationModel,
)
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import OrderSide
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    AutomationStateChangedPayload,
    BrokerOrderRecordedEnvelope,
    BrokerOrderStateChangedEnvelope,
    ExecutionLotAllocatedEnvelope,
    FactBrokerOrderStatus,
    FactEnvelope,
    FactIngressErrorCode,
    FactKind,
    PositionCycleUpdatedEnvelope,
    PositionLotOpenedEnvelope,
    TradeAuditRecordedEnvelope,
    TradeDecisionRecordedEnvelope,
    TradeExecutionRecordedEnvelope,
)
from tests.contracts.test_trading_facts_contract import all_envelopes
from tests.storage.test_trading_facts_models import automation_model
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

NOW = datetime(2026, 8, 13, 12, 0, 0, 123000, tzinfo=UTC)
SCOPE_ID = UUID("00000000-0000-4000-8000-000000000201")
AUTOMATION_A = UUID("00000000-0000-4000-8000-000000000202")
AUTOMATION_B = UUID("00000000-0000-4000-8000-000000000203")
INSTRUMENT_A = UUID("00000000-0000-4000-8000-000000000204")
INSTRUMENT_B = UUID("00000000-0000-4000-8000-000000000205")
EnvelopeT = TypeVar(
    "EnvelopeT",
    AutomationStateChangedEnvelope,
    BrokerOrderRecordedEnvelope,
    BrokerOrderStateChangedEnvelope,
    ExecutionLotAllocatedEnvelope,
    PositionCycleUpdatedEnvelope,
    PositionLotOpenedEnvelope,
    TradeAuditRecordedEnvelope,
    TradeDecisionRecordedEnvelope,
    TradeExecutionRecordedEnvelope,
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'fact-ingress.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
        session.flush()
        session.add_all(
            [
                instrument_model(str(INSTRUMENT_A), str(SCOPE_ID)),
                instrument_model(str(INSTRUMENT_B), str(SCOPE_ID)),
            ]
        )
        session.flush()
        session.add_all(
            [
                automation_model(
                    str(AUTOMATION_A),
                    user_broker_id=str(SCOPE_ID),
                    instrument_id=str(INSTRUMENT_A),
                ),
                automation_model(
                    str(AUTOMATION_B),
                    user_broker_id=str(SCOPE_ID),
                    instrument_id=str(INSTRUMENT_B),
                ),
            ]
        )
    yield engine, factory
    engine.dispose()


def state_fact(
    automation_id: UUID,
    *,
    event_id: UUID,
    sequence_number: int = 1,
    expected_revision: int = 1,
) -> AutomationStateChangedEnvelope:
    return AutomationStateChangedEnvelope(
        event_id=event_id,
        user_broker_id=SCOPE_ID,
        automation_id=automation_id,
        sequence_number=sequence_number,
        expected_revision=expected_revision,
        safe_message="Synthetic state transition",
        occurred_at=NOW,
        fact_kind=FactKind.AUTOMATION_STATE_CHANGED,
        payload=AutomationStateChangedPayload(
            state=AutomationState.HOLD,
            suspended_from_state=AutomationState.IN_WORK,
            hold_reason="synthetic hold",
            closed_at=None,
        ),
    )


def service(factory: sessionmaker[Session]) -> TradingFactIngressService:
    return TradingFactIngressService(
        lambda: TradingFactsUnitOfWork(factory),
        TradingFactMapper(),
        now=lambda: NOW,
    )


@pytest.mark.parametrize("initial_state", [AutomationState.CLOSED, AutomationState.HOLD])
def test_worker_cannot_reactivate_closed_or_user_held_automation(
    database: tuple[Engine, sessionmaker[Session]], initial_state: AutomationState
) -> None:
    _, factory = database
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        automation.state = initial_state.value
        automation.closed_at = NOW if initial_state is AutomationState.CLOSED else None
    activation = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000231"))
    activation = activation.model_copy(
        update={
            "payload": AutomationStateChangedPayload(
                state=AutomationState.IN_WORK, suspended_from_state=None, hold_reason=None, closed_at=None
            )
        }
    )

    result = service(factory).publish([activation])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    assert result.failures[0].retryable is False
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert automation.state == initial_state.value
        assert automation.revision == 1
        assert automation.last_sequence_number == 0
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


@pytest.mark.parametrize("initial_state", list(AutomationState))
def test_new_same_state_event_is_rejected_without_revision_or_sequence_change(
    database: tuple[Engine, sessionmaker[Session]], initial_state: AutomationState
) -> None:
    _, factory = database
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        automation.state = initial_state.value
        automation.closed_at = NOW if initial_state is AutomationState.CLOSED else None
    fact = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000232"))
    fact = fact.model_copy(
        update={
            "payload": fact.payload.model_copy(
                update={
                    "state": initial_state,
                    "suspended_from_state": AutomationState.IN_WORK if initial_state is AutomationState.HOLD else None,
                    "hold_reason": "USER_HOLD" if initial_state is AutomationState.HOLD else None,
                    "closed_at": NOW if initial_state is AutomationState.CLOSED else None,
                }
            )
        }
    )

    result = service(factory).publish([fact])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == (initial_state.value, 1, 0)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"state": AutomationState.CLOSED, "closed_at": None, "suspended_from_state": None, "hold_reason": None},
        {"closed_at": NOW},
        {"suspended_from_state": AutomationState.IN_QUEUE},
        {"hold_reason": None},
        {"hold_reason": "   "},
        {"state": AutomationState.CLOSED, "closed_at": NOW},
    ],
)
def test_state_payload_metadata_is_rejected_before_persistence(
    database: tuple[Engine, sessionmaker[Session]], changes: dict[str, object]
) -> None:
    _, factory = database
    fact = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000233"))
    fact = fact.model_copy(update={"payload": fact.payload.model_copy(update=changes)})

    result = service(factory).publish([fact])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    assert result.failures[0].retryable is False
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 1, 0)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


def test_invalid_state_rolls_back_preceding_supporting_fact(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    with factory.begin() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        automation.state = AutomationState.HOLD.value
    cycle = complete_fact_sequence()[0]
    activation = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000234"), sequence_number=2)
    activation = activation.model_copy(
        update={
            "payload": activation.payload.model_copy(
                update={
                    "state": AutomationState.IN_WORK,
                    "suspended_from_state": None,
                    "hold_reason": None,
                }
            )
        }
    )

    result = service(factory).publish([cycle, activation])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("HOLD", 1, 0)
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 0
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


def test_closed_automation_accepts_delayed_supporting_facts_and_exact_state_retry(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    close = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000235"))
    close = close.model_copy(
        update={
            "payload": close.payload.model_copy(
                update={
                    "state": AutomationState.CLOSED,
                    "closed_at": NOW,
                    "suspended_from_state": None,
                    "hold_reason": None,
                }
            )
        }
    )
    assert service(factory).publish([close]).failures == ()
    cycle = complete_fact_sequence()[0].model_copy(update={"sequence_number": 2, "expected_revision": 2})

    result = service(factory).publish([close, cycle])

    assert result.failures == ()
    assert result.results[0].accepted_event_ids == (close.event_id, cycle.event_id)
    assert result.results[0].current_revision == 2
    assert result.results[0].accepted_through_sequence == 2
    with factory() as session:
        assert session.get_one(TradingAutomationModel, str(AUTOMATION_A)).state == "CLOSED"
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 1
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 2


def test_publish_accepts_state_fact_and_exact_retry_once(database: tuple[Engine, sessionmaker[Session]]) -> None:
    _, factory = database
    fact = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000211"))

    first = service(factory).publish([fact])
    second = service(factory).publish([fact])

    assert first.failures == second.failures == ()
    assert first.results[0].accepted_through_sequence == 1
    assert first.results[0].current_revision == 2
    assert second.results[0].accepted_event_ids == (fact.event_id,)
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        assert automation is not None
        assert automation.revision == 2
        assert automation.last_sequence_number == 1
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 1


def test_failed_group_does_not_rollback_successful_peer(database: tuple[Engine, sessionmaker[Session]]) -> None:
    _, factory = database
    valid = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000212"))
    missing = state_fact(
        UUID("00000000-0000-4000-8000-000000000299"),
        event_id=UUID("00000000-0000-4000-8000-000000000213"),
    )

    result = service(factory).publish([missing, valid])

    assert tuple(item.automation_id for item in result.results) == (AUTOMATION_A,)
    assert result.failures[0].automation_id == missing.automation_id
    assert result.failures[0].code is FactIngressErrorCode.AUTOMATION_NOT_FOUND
    assert result.failures[0].retryable is False
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 1


def test_sequence_gap_rolls_back_entire_group(database: tuple[Engine, sessionmaker[Session]]) -> None:
    _, factory = database
    gap = state_fact(
        AUTOMATION_A,
        event_id=UUID("00000000-0000-4000-8000-000000000214"),
        sequence_number=2,
    )

    result = service(factory).publish([gap])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.AUTOMATION_SEQUENCE_GAP
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        assert automation is not None
        assert automation.revision == 1
        assert automation.last_sequence_number == 0
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


def _contract_envelope(envelope_type: type[EnvelopeT]) -> EnvelopeT:
    return next(value for value in all_envelopes() if isinstance(value, envelope_type))


def complete_fact_sequence() -> list[FactEnvelope]:
    cycle = _contract_envelope(PositionCycleUpdatedEnvelope)
    decision = _contract_envelope(TradeDecisionRecordedEnvelope)
    order = _contract_envelope(BrokerOrderRecordedEnvelope)
    order_state = _contract_envelope(BrokerOrderStateChangedEnvelope)
    execution = _contract_envelope(TradeExecutionRecordedEnvelope)
    lot = _contract_envelope(PositionLotOpenedEnvelope)
    allocation = _contract_envelope(ExecutionLotAllocatedEnvelope)
    audit = _contract_envelope(TradeAuditRecordedEnvelope)
    state = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000236"))
    instrument = INSTRUMENT_A
    cycle_id = cycle.payload.position_cycle_id
    decision_id = decision.payload.decision_id
    order_id = order.payload.order_id
    buy_execution_id = execution.payload.execution_id
    sell_execution_id = UUID("00000000-0000-4000-8000-000000000221")
    values = [
        cycle.model_copy(update={"payload": cycle.payload.model_copy(update={"instrument_id": instrument})}),
        decision.model_copy(
            update={
                "payload": decision.payload.model_copy(
                    update={"position_cycle_id": cycle_id, "instrument_id": instrument}
                )
            }
        ),
        order.model_copy(
            update={
                "payload": order.payload.model_copy(
                    update={
                        "decision_id": decision_id,
                        "position_cycle_id": cycle_id,
                        "instrument_id": instrument,
                    }
                )
            }
        ),
        order_state.model_copy(
            update={
                "payload": order_state.payload.model_copy(
                    update={
                        "order_id": order_id,
                        "broker_order_id": order_id,
                        "decision_id": decision_id,
                        "position_cycle_id": cycle_id,
                        "instrument_id": instrument,
                        "state": order_state.payload.to_state,
                    }
                )
            }
        ),
        execution.model_copy(
            update={
                "payload": execution.payload.model_copy(
                    update={
                        "broker_order_id": order_id,
                        "position_cycle_id": cycle_id,
                        "instrument_id": instrument,
                    }
                )
            }
        ),
        lot.model_copy(
            update={
                "payload": lot.payload.model_copy(
                    update={"position_cycle_id": cycle_id, "buy_execution_id": buy_execution_id}
                )
            }
        ),
        execution.model_copy(
            update={
                "event_id": UUID("00000000-0000-4000-8000-000000000222"),
                "payload": execution.payload.model_copy(
                    update={
                        "execution_id": sell_execution_id,
                        "broker_order_id": order_id,
                        "position_cycle_id": cycle_id,
                        "instrument_id": instrument,
                        "external_execution_id": "synthetic-sell-execution",
                        "side": OrderSide.SELL,
                    }
                ),
            }
        ),
        allocation.model_copy(
            update={
                "payload": allocation.payload.model_copy(
                    update={
                        "position_cycle_id": cycle_id,
                        "sell_execution_id": sell_execution_id,
                        "position_lot_id": lot.payload.position_lot_id,
                    }
                )
            }
        ),
        audit.model_copy(
            update={
                "payload": audit.payload.model_copy(
                    update={
                        "decision_id": decision_id,
                        "broker_order_id": order_id,
                        "execution_id": sell_execution_id,
                        "instrument_id": instrument,
                    }
                )
            }
        ),
        state,
    ]
    adapter: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)
    return [
        adapter.validate_python(
            {
                **value.model_dump(mode="python"),
                "user_broker_id": SCOPE_ID,
                "automation_id": AUTOMATION_A,
                "sequence_number": sequence_number,
            }
        )
        for sequence_number, value in enumerate(values, start=1)
    ]


def test_publish_maps_all_nine_fact_kinds_in_one_transaction(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database

    result = service(factory).publish(complete_fact_sequence())

    assert result.failures == ()
    assert result.results[0].accepted_through_sequence == 10
    assert result.results[0].current_revision == 2
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        order = session.scalar(select(BrokerOrderModel))
        assert automation is not None
        assert automation.last_sequence_number == 10
        assert order is not None
        assert order.state == BrokerOrderStatus.SUBMITTING.value
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 10
        for model, expected in (
            (PositionCycleModel, 1),
            (TradeDecisionModel, 1),
            (BrokerOrderModel, 1),
            (BrokerOrderEventModel, 1),
            (TradeExecutionModel, 2),
            (PositionLotModel, 1),
            (ExecutionLotAllocationModel, 1),
            (TradeAuditEventModel, 1),
        ):
            assert session.scalar(select(func.count()).select_from(model)) == expected


def test_order_transition_rejects_mismatched_from_state_without_mutation(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    facts = complete_fact_sequence()[:3]
    accepted = service(factory).publish(facts)
    assert accepted.failures == ()
    transition = complete_fact_sequence()[3]
    assert isinstance(transition, BrokerOrderStateChangedEnvelope)
    invalid = transition.model_copy(
        update={
            "sequence_number": 4,
            "payload": transition.payload.model_copy(update={"from_state": FactBrokerOrderStatus.FILLED}),
        }
    )

    result = service(factory).publish([invalid])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    with factory() as session:
        order = session.scalar(select(BrokerOrderModel))
        assert order is not None
        assert order.state == facts[2].payload.state.value
        assert session.scalar(select(func.count()).select_from(BrokerOrderEventModel)) == 0


def test_order_transition_rejects_terminal_state_regression(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    facts = complete_fact_sequence()[:4]
    transition = facts[3]
    assert isinstance(transition, BrokerOrderStateChangedEnvelope)
    terminal = transition.model_copy(
        update={
            "payload": transition.payload.model_copy(
                update={
                    "from_state": facts[2].payload.state,
                    "state": FactBrokerOrderStatus.FILLED,
                    "to_state": FactBrokerOrderStatus.FILLED,
                }
            )
        }
    )
    assert service(factory).publish([*facts[:3], terminal]).failures == ()
    regression = terminal.model_copy(
        update={
            "event_id": UUID("00000000-0000-4000-8000-000000000225"),
            "sequence_number": 5,
            "expected_revision": 1,
            "payload": terminal.payload.model_copy(
                update={
                    "order_event_id": UUID("00000000-0000-4000-8000-000000000226"),
                    "from_state": FactBrokerOrderStatus.FILLED,
                    "state": FactBrokerOrderStatus.SUBMITTED,
                    "to_state": FactBrokerOrderStatus.SUBMITTED,
                }
            ),
        }
    )

    result = service(factory).publish([regression])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    with factory() as session:
        order = session.scalar(select(BrokerOrderModel))
        assert order is not None
        assert order.state == BrokerOrderStatus.FILLED.value


def test_terminal_order_repair_accepts_stale_local_state_without_duplicate_transition(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    facts = complete_fact_sequence()[:4]
    transition = facts[3]
    assert isinstance(transition, BrokerOrderStateChangedEnvelope)
    terminal = transition.model_copy(
        update={
            "payload": transition.payload.model_copy(
                update={
                    "from_state": facts[2].payload.state,
                    "state": FactBrokerOrderStatus.FILLED,
                    "to_state": FactBrokerOrderStatus.FILLED,
                }
            )
        }
    )
    assert service(factory).publish([*facts[:3], terminal]).failures == ()
    repair = terminal.model_copy(
        update={
            "event_id": UUID("00000000-0000-4000-8000-000000000227"),
            "sequence_number": 5,
            "payload": terminal.payload.model_copy(
                update={
                    "order_event_id": UUID("00000000-0000-4000-8000-000000000228"),
                    "from_state": FactBrokerOrderStatus.UNCERTAIN,
                    "executed_amount": Decimal("212.34"),
                    "executed_commission": Decimal("0.42"),
                    "executed_at": NOW,
                    "terminal_at": NOW,
                }
            ),
        }
    )

    result = service(factory).publish([repair])

    assert result.failures == ()
    assert result.results[0].accepted_through_sequence == 5
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        order = session.scalar(select(BrokerOrderModel))
        assert automation is not None
        assert automation.last_sequence_number == 5
        assert order is not None
        assert order.state == BrokerOrderStatus.FILLED.value
        assert order.executed_amount == Decimal("212.34")
        assert order.executed_commission == Decimal("0.42")
        assert session.scalar(select(func.count()).select_from(BrokerOrderEventModel)) == 1


def test_revision_conflict_after_first_fact_rolls_back_entire_group(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    first = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000223"))
    stale = state_fact(
        AUTOMATION_A,
        event_id=UUID("00000000-0000-4000-8000-000000000224"),
        sequence_number=2,
        expected_revision=1,
    )

    result = service(factory).publish([first, stale])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        assert automation is not None
        assert automation.revision == 1
        assert automation.last_sequence_number == 0
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


def assert_cycle_instrument_lineage_rejects_group_atomically(factory: sessionmaker[Session]) -> None:
    first = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000251"))
    cycle = complete_fact_sequence()[0]
    assert isinstance(cycle, PositionCycleUpdatedEnvelope)
    matching = cycle.model_copy(update={"sequence_number": 2, "expected_revision": 2})
    mismatched = matching.model_copy(
        update={"payload": matching.payload.model_copy(update={"instrument_id": INSTRUMENT_B})}
    )

    rejected = service(factory).publish([first, mismatched])

    assert rejected.results == ()
    assert len(rejected.failures) == 1
    assert rejected.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    assert rejected.failures[0].retryable is False
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert automation.bootstrap_position_cycle_id is None
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 1, 0)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 0

    accepted = service(factory).publish([first, matching])
    replayed = service(factory).publish([first, matching])

    assert accepted.failures == replayed.failures == ()
    assert accepted.results == replayed.results
    assert accepted.results[0].accepted_event_ids == (first.event_id, matching.event_id)
    assert accepted.results[0].current_revision == 2
    assert accepted.results[0].accepted_through_sequence == 2
    with factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("HOLD", 2, 2)
        stored = session.get_one(PositionCycleModel, str(matching.payload.position_cycle_id))
        assert stored.automation_id == str(AUTOMATION_A)
        assert stored.instrument_id == str(INSTRUMENT_A)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 2
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 1


def test_cycle_instrument_mismatch_rolls_back_group_and_allows_corrected_replay(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    assert_cycle_instrument_lineage_rejects_group_atomically(factory)


def test_reused_event_id_with_changed_content_is_rejected_without_reapply(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    fact = state_fact(AUTOMATION_A, event_id=UUID("00000000-0000-4000-8000-000000000225"))
    assert service(factory).publish([fact]).failures == ()

    result = service(factory).publish([fact.model_copy(update={"safe_message": "Different synthetic message"})])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.FACT_ID_CONFLICT
    with factory() as session:
        automation = session.get(TradingAutomationModel, str(AUTOMATION_A))
        assert automation is not None
        assert automation.revision == 2
        assert automation.last_sequence_number == 1
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 1
