"""Durable Worker schema and queue for unified typed fact delivery."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationStateChangedPayload, FactKind, TradeAuditRecordedPayload
from tests.contracts.trading_facts_helpers import all_envelopes
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, CachedAutomationModel, FactOutboxModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 13, 12, 0, 0, 123000, tzinfo=UTC)
SCOPE_ID = "00000000-0000-4000-8000-000000000302"
AUTOMATION_A = "00000000-0000-4000-8000-000000000303"
AUTOMATION_B = "00000000-0000-4000-8000-000000000305"
INSTRUMENT_ID = "00000000-0000-4000-8000-000000000306"


def cached_automation(automation_id: str) -> CachedAutomationModel:
    return CachedAutomationModel(
        automation_id=automation_id,
        user_broker_id=SCOPE_ID,
        broker_id="00000000-0000-4000-8000-000000000307",
        account_id="synthetic-account",
        instrument_id="synthetic-instrument",
        fact_instrument_id=INSTRUMENT_ID,
        currency="RUB",
        position_cycle_id=None,
        lot_size=10,
        min_price_increment="0.01",
        state="IN_WORK",
        revision=1,
        last_sequence_number=0,
        resume_requested=False,
    )


def state_payload() -> AutomationStateChangedPayload:
    return AutomationStateChangedPayload(
        state=AutomationState.HOLD,
        suspended_from_state=AutomationState.IN_WORK,
        hold_reason="synthetic hold",
        closed_at=None,
    )


def audit_payload() -> TradeAuditRecordedPayload:
    return next(envelope.payload for envelope in all_envelopes() if envelope.fact_kind is FactKind.TRADE_AUDIT_RECORDED)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'writer.db'}")
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result.begin() as session:
        session.add_all([cached_automation(AUTOMATION_A), cached_automation(AUTOMATION_B)])
    yield result
    engine.dispose()


def test_fact_outbox_schema_persists_mixed_kinds_across_reopen(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fact-outbox.db'}"
    engine = create_worker_engine(database_url)
    Base.metadata.create_all(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("fact_outbox")}
    assert columns == {
        "event_id",
        "user_broker_id",
        "automation_id",
        "sequence_number",
        "expected_revision",
        "fact_kind",
        "payload",
        "safe_message",
        "occurred_at",
        "delivery_state",
        "retry_count",
        "next_retry_at",
        "created_at",
        "updated_at",
    }
    with Session(engine) as session:
        session.add_all(
            [
                FactOutboxModel(
                    event_id="00000000-0000-4000-8000-000000000301",
                    user_broker_id="00000000-0000-4000-8000-000000000302",
                    automation_id="00000000-0000-4000-8000-000000000303",
                    sequence_number=1,
                    expected_revision=1,
                    fact_kind="AUTOMATION_STATE_CHANGED",
                    payload={"state": "IN_WORK"},
                    safe_message="Synthetic state",
                    occurred_at=NOW,
                ),
                FactOutboxModel(
                    event_id="00000000-0000-4000-8000-000000000304",
                    user_broker_id="00000000-0000-4000-8000-000000000302",
                    automation_id="00000000-0000-4000-8000-000000000303",
                    sequence_number=2,
                    expected_revision=2,
                    fact_kind="TRADE_AUDIT_RECORDED",
                    payload={"stage": "SYNTHETIC"},
                    safe_message="Synthetic audit",
                    occurred_at=NOW,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    reopened = create_worker_engine(database_url)
    with Session(reopened) as session:
        rows = session.query(FactOutboxModel).order_by(FactOutboxModel.sequence_number).all()
        assert [row.fact_kind for row in rows] == ["AUTOMATION_STATE_CHANGED", "TRADE_AUDIT_RECORDED"]
        assert all(row.delivery_state == "PENDING" for row in rows)
        assert all(row.retry_count == 0 for row in rows)
        assert all(row.created_at.microsecond % 1000 == 0 for row in rows)
    reopened.dispose()


def test_writer_allocates_continuous_sequence_and_revision_in_one_session(
    factory: sessionmaker[Session],
) -> None:
    ids = iter(
        [
            UUID("00000000-0000-4000-8000-000000000311"),
            UUID("00000000-0000-4000-8000-000000000312"),
        ]
    )
    writer = FactOutboxWriter(id_factory=lambda: next(ids))
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        first = writer.append(
            session,
            cached,
            payload=state_payload(),
            safe_message="Synthetic state",
            occurred_at=NOW.replace(microsecond=123999),
            changes_revision=True,
        )
        second = writer.append(
            session,
            cached,
            payload=audit_payload(),
            safe_message="Synthetic audit",
            occurred_at=NOW,
        )

    assert first.sequence_number == 1
    assert first.expected_revision == 1
    assert first.occurred_at.microsecond == 123000
    assert second.sequence_number == 2
    assert second.expected_revision == 2
    with factory() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        assert cached.last_sequence_number == 2
        assert cached.revision == 2
        rows = session.scalars(select(FactOutboxModel).order_by(FactOutboxModel.sequence_number)).all()
        assert [row.fact_kind for row in rows] == [
            FactKind.AUTOMATION_STATE_CHANGED.value,
            FactKind.TRADE_AUDIT_RECORDED.value,
        ]


def test_writer_rolls_back_with_recovery_mutation(factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        append_then_fail(factory)

    with factory() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        assert cached.state == "IN_WORK"
        assert cached.last_sequence_number == 0
        assert cached.revision == 1
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def append_then_fail(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.state = "HOLD"
        FactOutboxWriter().append(
            session,
            cached,
            payload=state_payload(),
            safe_message="Synthetic state",
            occurred_at=NOW,
            changes_revision=True,
        )
        raise RuntimeError("synthetic rollback")


def append_fact(
    factory: sessionmaker[Session],
    *,
    automation_id: str,
    event_id: UUID,
    occurred_at: datetime,
) -> None:
    writer = FactOutboxWriter(id_factory=lambda: event_id, clock=lambda: NOW)
    with factory.begin() as session:
        writer.append(
            session,
            session.get_one(CachedAutomationModel, automation_id),
            payload=audit_payload(),
            safe_message="Synthetic audit",
            occurred_at=occurred_at,
        )


def test_ready_queue_uses_global_count_or_deadline(factory: sessionmaker[Session]) -> None:
    append_fact(
        factory,
        automation_id=AUTOMATION_A,
        event_id=UUID("00000000-0000-4000-8000-000000000321"),
        occurred_at=NOW,
    )
    append_fact(
        factory,
        automation_id=AUTOMATION_B,
        event_id=UUID("00000000-0000-4000-8000-000000000322"),
        occurred_at=NOW + timedelta(milliseconds=1),
    )
    repository = LocalAutomationRepository(factory)

    assert repository.ready_fact_outbox(3, now=NOW + timedelta(milliseconds=50), deadline_ms=100) == []
    deadline_batch = repository.ready_fact_outbox(3, now=NOW + timedelta(milliseconds=100), deadline_ms=100)
    count_batch = repository.ready_fact_outbox(2, now=NOW + timedelta(milliseconds=1), deadline_ms=100)

    assert [row.automation_id for row in deadline_batch] == [AUTOMATION_A, AUTOMATION_B]
    assert [row.event_id for row in count_batch] == [
        "00000000-0000-4000-8000-000000000321",
        "00000000-0000-4000-8000-000000000322",
    ]


def test_acknowledgement_and_retry_are_selective(factory: sessionmaker[Session]) -> None:
    append_fact(
        factory,
        automation_id=AUTOMATION_A,
        event_id=UUID("00000000-0000-4000-8000-000000000331"),
        occurred_at=NOW,
    )
    append_fact(
        factory,
        automation_id=AUTOMATION_A,
        event_id=UUID("00000000-0000-4000-8000-000000000332"),
        occurred_at=NOW + timedelta(milliseconds=1),
    )
    append_fact(
        factory,
        automation_id=AUTOMATION_B,
        event_id=UUID("00000000-0000-4000-8000-000000000333"),
        occurred_at=NOW + timedelta(milliseconds=2),
    )
    repository = LocalAutomationRepository(factory)
    retry_at = NOW + timedelta(seconds=1)

    repository.schedule_fact_retry(
        ("00000000-0000-4000-8000-000000000332",),
        retry_count=2,
        next_retry_at=retry_at,
    )
    repository.acknowledge_fact_outbox(
        AUTOMATION_A,
        accepted_through_sequence=1,
        current_revision=4,
    )

    with factory() as session:
        rows = session.scalars(select(FactOutboxModel).order_by(FactOutboxModel.event_id)).all()
        assert [row.event_id for row in rows] == [
            "00000000-0000-4000-8000-000000000332",
            "00000000-0000-4000-8000-000000000333",
        ]
        assert rows[0].retry_count == 2
        assert rows[0].next_retry_at == retry_at
        assert session.get_one(CachedAutomationModel, AUTOMATION_A).revision == 4


def test_partial_ack_preserves_pending_state_revision_for_a_new_fact(factory: sessionmaker[Session]) -> None:
    writer = FactOutboxWriter(clock=lambda: NOW)
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        writer.append(session, cached, payload=audit_payload(), safe_message="Audit", occurred_at=NOW)
        writer.append(
            session,
            cached,
            payload=state_payload(),
            safe_message="State",
            occurred_at=NOW,
            changes_revision=True,
        )
    repository = LocalAutomationRepository(factory)

    repository.acknowledge_fact_outbox(AUTOMATION_A, accepted_through_sequence=1, current_revision=1)
    with factory.begin() as session:
        appended = writer.append(
            session,
            session.get_one(CachedAutomationModel, AUTOMATION_A),
            payload=audit_payload(),
            safe_message="Later audit",
            occurred_at=NOW,
        )

    assert appended.sequence_number == 3
    assert appended.expected_revision == 2
    rows = repository.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    assert [(row.sequence_number, row.expected_revision) for row in rows] == [(2, 1), (3, 2)]


def test_retrying_head_blocks_only_same_automation_suffix(factory: sessionmaker[Session]) -> None:
    first_id = UUID("00000000-0000-4000-8000-000000000341")
    second_id = UUID("00000000-0000-4000-8000-000000000342")
    peer_id = UUID("00000000-0000-4000-8000-000000000343")
    append_fact(factory, automation_id=AUTOMATION_A, event_id=first_id, occurred_at=NOW)
    append_fact(
        factory,
        automation_id=AUTOMATION_A,
        event_id=second_id,
        occurred_at=NOW + timedelta(milliseconds=1),
    )
    append_fact(
        factory,
        automation_id=AUTOMATION_B,
        event_id=peer_id,
        occurred_at=NOW + timedelta(milliseconds=2),
    )
    repository = LocalAutomationRepository(factory)
    repository.schedule_fact_retry(
        (str(first_id),),
        retry_count=1,
        next_retry_at=NOW + timedelta(seconds=1),
    )

    rows = repository.ready_fact_outbox(10, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [str(peer_id)]
