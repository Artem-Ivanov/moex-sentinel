"""Durable Worker schema and queue for unified typed fact delivery."""

import gzip
import importlib
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copyfileobj
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from develop.benchmarks.fact_outbox_baseline_oracle import select_old_batch
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


def insert_outbox_rows(factory, rows: list[FactOutboxModel]) -> None:
    with factory.begin() as session:
        session.add_all(rows)


def outbox_row(event_number: int, automation_id: str, sequence: int, **overrides) -> FactOutboxModel:
    values = {
        "event_id": str(UUID(int=event_number)),
        "user_broker_id": SCOPE_ID,
        "automation_id": automation_id,
        "sequence_number": sequence,
        "expected_revision": 1,
        "fact_kind": FactKind.TRADE_AUDIT_RECORDED.value,
        "payload": {"stage": "SYNTHETIC"},
        "safe_message": "Synthetic audit",
        "occurred_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "delivery_state": "PENDING",
        "retry_count": 0,
        "next_retry_at": None,
    }
    values.update(overrides)
    return FactOutboxModel(**values)


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


@pytest.mark.parametrize("blocked_sequence", [pytest.param(1, id="head"), pytest.param(2, id="middle")])
@pytest.mark.parametrize(
    "retry_offset_ms",
    [pytest.param(-1, id="before-due"), pytest.param(0, id="exactly-due"), pytest.param(1, id="after-due")],
)
def test_small_batch_preserves_retry_prefix_and_peer_progress(factory, blocked_sequence, retry_offset_ms):
    ids = [UUID(int=400 + index) for index in range(4)]
    for index in range(3):
        append_fact(
            factory, automation_id=AUTOMATION_A, event_id=ids[index], occurred_at=NOW + timedelta(milliseconds=index)
        )
    append_fact(factory, automation_id=AUTOMATION_B, event_id=ids[3], occurred_at=NOW + timedelta(milliseconds=3))
    repository = LocalAutomationRepository(factory)
    retry_at = NOW + timedelta(seconds=1)
    repository.schedule_fact_retry((str(ids[blocked_sequence - 1]),), retry_count=1, next_retry_at=retry_at)

    rows = repository.ready_fact_outbox(2, now=retry_at + timedelta(milliseconds=retry_offset_ms), deadline_ms=0)

    expected = ids[:2] if retry_offset_ms >= 0 else (ids[:1] + ids[3:] if blocked_sequence == 2 else ids[3:])
    assert [row.event_id for row in rows] == [str(value) for value in expected]


@pytest.mark.parametrize(
    "blocked_sequence", [pytest.param(1, id="new-head-retry"), pytest.param(2, id="new-middle-retry")]
)
def test_ready_batch_observes_one_snapshot_during_concurrent_retry_commit(factory, blocked_sequence):
    ids = [UUID(int=500 + index) for index in range(3)]
    append_fact(factory, automation_id=AUTOMATION_A, event_id=ids[0], occurred_at=NOW)
    append_fact(factory, automation_id=AUTOMATION_A, event_id=ids[1], occurred_at=NOW + timedelta(milliseconds=1))
    append_fact(factory, automation_id=AUTOMATION_B, event_id=ids[2], occurred_at=NOW + timedelta(milliseconds=2))
    repository = LocalAutomationRepository(factory)
    engine = factory.kw["bind"]
    committed = False

    def concurrent_retry(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal committed
        if committed or not statement.lstrip().upper().startswith("SELECT"):
            return
        committed = True
        # Independent connection commits after the reader's first SELECT, before later reads.
        repository.schedule_fact_retry(
            (str(ids[blocked_sequence - 1]),), retry_count=1, next_retry_at=NOW + timedelta(seconds=1)
        )

    event.listen(engine, "after_cursor_execute", concurrent_retry)
    try:
        first = repository.ready_fact_outbox(2, now=NOW, deadline_ms=0)
    finally:
        event.remove(engine, "after_cursor_execute", concurrent_retry)

    assert committed is True
    assert [row.event_id for row in first] == [str(ids[0]), str(ids[1])]
    next_batch = repository.ready_fact_outbox(2, now=NOW, deadline_ms=0)
    expected = [ids[2]] if blocked_sequence == 1 else [ids[0], ids[2]]
    assert [row.event_id for row in next_batch] == [str(value) for value in expected]


@pytest.mark.parametrize(("limit", "expected_count"), [(0, 0), (1, 1), (2, 2), (100, 3)])
def test_selector_characterizes_limits_on_persisted_sqlite(factory, limit, expected_count):
    ids = [UUID(int=600 + index) for index in range(3)]
    insert_outbox_rows(
        factory,
        [outbox_row(value.int, AUTOMATION_A, index + 1) for index, value in enumerate(ids)],
    )

    rows = LocalAutomationRepository(factory).ready_fact_outbox(limit, now=NOW, deadline_ms=0)

    assert len(rows) == expected_count
    assert [row.event_id for row in rows] == [str(value) for value in ids[:expected_count]]
    assert [row.event_id for row in rows] == select_old_batch(factory, limit, now=NOW, deadline_ms=0)


def test_selector_characterizes_bootstrap_quartet_and_separate_hold(factory):
    cycle_id = "00000000-0000-4000-8000-000000000701"
    lot_id = "00000000-0000-4000-8000-000000000702"
    quartet_kinds = [
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.POSITION_LOT_OPENED.value,
        FactKind.TRADE_AUDIT_RECORDED.value,
        FactKind.AUTOMATION_STATE_CHANGED.value,
    ]
    payloads = [
        {"position_cycle_id": cycle_id},
        {
            "position_cycle_id": cycle_id,
            "position_lot_id": lot_id,
            "source": "BROKER_POSITION_BOOTSTRAP",
        },
        {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
        {"state": "IN_WORK"},
    ]
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        session.add_all(
            [
                outbox_row(710 + index, AUTOMATION_A, index + 1, fact_kind=kind, payload=payload)
                for index, (kind, payload) in enumerate(zip(quartet_kinds, payloads, strict=True))
            ]
            + [
                outbox_row(
                    714,
                    AUTOMATION_A,
                    5,
                    fact_kind=FactKind.AUTOMATION_STATE_CHANGED.value,
                    payload={"state": "HOLD"},
                )
            ]
        )

    repository = LocalAutomationRepository(factory)
    rows = repository.ready_fact_outbox(2, now=NOW, deadline_ms=0)

    assert [row.sequence_number for row in rows] == [1, 2, 3, 4]
    assert [row.event_id for row in rows] == [str(UUID(int=710 + index)) for index in range(4)]
    assert [row.event_id for row in rows] == select_old_batch(factory, 2, now=NOW, deadline_ms=0)

    repository.acknowledge_fact_outbox(AUTOMATION_A, accepted_through_sequence=4, current_revision=4)
    hold = repository.ready_fact_outbox(100, now=NOW, deadline_ms=0)
    assert [(row.sequence_number, row.payload["state"]) for row in hold] == [(5, "HOLD")]


@pytest.mark.parametrize("invalid_part", ["sequence", "payload"])
def test_selector_does_not_group_invalid_bootstrap_quartet(factory, invalid_part):
    cycle_id = "00000000-0000-4000-8000-000000000711"
    lot_id = "00000000-0000-4000-8000-000000000712"
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        for index, kind in enumerate(
            [
                FactKind.POSITION_CYCLE_UPDATED.value,
                FactKind.POSITION_LOT_OPENED.value,
                FactKind.TRADE_AUDIT_RECORDED.value,
                FactKind.AUTOMATION_STATE_CHANGED.value,
            ]
        ):
            payload = [
                {"position_cycle_id": cycle_id},
                {
                    "position_cycle_id": cycle_id,
                    "position_lot_id": lot_id,
                    "source": "BROKER_POSITION_BOOTSTRAP",
                },
                {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
                {"state": "IN_WORK"},
            ][index]
            sequence = index + 1
            if invalid_part == "sequence" and index >= 2:
                sequence += 1
            if invalid_part == "payload" and index == 0:
                payload = {"position_cycle_id": "wrong-cycle"}
            session.add(outbox_row(730 + index, AUTOMATION_A, sequence, fact_kind=kind, payload=payload))

    rows = LocalAutomationRepository(factory).ready_fact_outbox(2, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [str(UUID(int=730)), str(UUID(int=731))]
    assert [row.event_id for row in rows] == select_old_batch(factory, 2, now=NOW, deadline_ms=0)


def test_selector_allows_quartet_to_extend_limit_by_three(factory):
    cycle_id = "00000000-0000-4000-8000-000000000721"
    lot_id = "00000000-0000-4000-8000-000000000722"
    kinds = [
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.POSITION_LOT_OPENED.value,
        FactKind.TRADE_AUDIT_RECORDED.value,
        FactKind.AUTOMATION_STATE_CHANGED.value,
    ]
    payloads = [
        {"position_cycle_id": cycle_id},
        {
            "position_cycle_id": cycle_id,
            "position_lot_id": lot_id,
            "source": "BROKER_POSITION_BOOTSTRAP",
        },
        {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
        {"state": "IN_WORK"},
    ]
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        session.add_all(
            [
                outbox_row(
                    800 + index,
                    AUTOMATION_B,
                    index + 1,
                    occurred_at=NOW + timedelta(milliseconds=index),
                )
                for index in range(99)
            ]
            + [
                outbox_row(
                    900 + index,
                    AUTOMATION_A,
                    index + 1,
                    fact_kind=kinds[index],
                    payload=payloads[index],
                    occurred_at=NOW + timedelta(milliseconds=200),
                )
                for index in range(4)
            ]
        )

    rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [
        *(str(UUID(int=800 + index)) for index in range(99)),
        *(str(UUID(int=900 + index)) for index in range(4)),
    ]
    assert [row.event_id for row in rows] == select_old_batch(factory, 100, now=NOW, deadline_ms=0)


def test_selector_characterizes_delayed_failed_and_blocked_heads(factory):
    delayed_head = outbox_row(720, AUTOMATION_A, 1, next_retry_at=NOW + timedelta(seconds=1))
    delayed_suffix = outbox_row(721, AUTOMATION_A, 2)
    failed_peer = outbox_row(722, AUTOMATION_B, 1, delivery_state="FAILED")
    ready_peer = outbox_row(723, INSTRUMENT_ID, 1)
    insert_outbox_rows(factory, [delayed_head, delayed_suffix, failed_peer, ready_peer])

    rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [str(UUID(int=723))]


def test_failed_fact_is_excluded_without_creating_sequence_gap(factory):
    failed = outbox_row(724, AUTOMATION_B, 1, delivery_state="FAILED")
    independent_pending = outbox_row(725, AUTOMATION_A, 1)
    insert_outbox_rows(factory, [failed, independent_pending])

    rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [str(UUID(int=725))]


def test_failed_head_blocks_same_automation_suffix(factory):
    insert_outbox_rows(
        factory,
        [
            outbox_row(726, AUTOMATION_B, 1, delivery_state="FAILED"),
            outbox_row(727, AUTOMATION_B, 2),
        ],
    )

    rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)

    assert rows == []


@pytest.mark.parametrize("backlog", [100, 10_000, 50_000])
def test_bootstrap_selector_retains_only_bounded_rows(factory, backlog, record_property):
    cycle_id = str(UUID(int=9001))
    lot_id = str(UUID(int=9002))
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        session.add_all(
            [outbox_row(100_000 + index, AUTOMATION_B, index + 1) for index in range(backlog)]
            + [
                outbox_row(
                    20_000,
                    AUTOMATION_A,
                    1,
                    fact_kind=FactKind.POSITION_CYCLE_UPDATED.value,
                    payload={"position_cycle_id": cycle_id},
                ),
                outbox_row(
                    20_001,
                    AUTOMATION_A,
                    2,
                    fact_kind=FactKind.POSITION_LOT_OPENED.value,
                    payload={
                        "position_cycle_id": cycle_id,
                        "position_lot_id": lot_id,
                        "source": "BROKER_POSITION_BOOTSTRAP",
                    },
                ),
                outbox_row(20_002, AUTOMATION_A, 3, payload={"stage": "BOOTSTRAP_POSITION_ADOPTED"}),
                outbox_row(
                    20_003,
                    AUTOMATION_A,
                    4,
                    fact_kind=FactKind.AUTOMATION_STATE_CHANGED.value,
                    payload={"state": "IN_WORK"},
                ),
            ]
        )

    retained_peak = 0
    metadata_peak = 0
    identity_peak = 0
    payload_peak = 0

    def trace(frame, event, _arg):
        nonlocal retained_peak, metadata_peak, identity_peak, payload_peak
        if event != "line" or not frame.f_code.co_filename.endswith("repository.py"):
            return trace
        local = frame.f_locals
        retained_peak = max((retained_peak, *(len(value) for value in local.values() if isinstance(value, list))))
        if frame.f_code.co_name == "ready_fact_outbox":
            metadata_peak = max(
                metadata_peak,
                local.get("candidate_rows", 0)
                + local.get("local_rows", 0)
                + len(local.get("unit", ()))
                + len(local.get("selected", ())),
            )
        payloads = local.get("payloads")
        if isinstance(payloads, dict):
            payload_peak = max(payload_peak, len(payloads))
        session = local.get("session")
        if isinstance(session, Session):
            identity_peak = max(identity_peak, len(session.identity_map))
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        rows = LocalAutomationRepository(factory).ready_fact_outbox(10, now=NOW, deadline_ms=0)
    finally:
        sys.settrace(previous)

    assert [row.event_id for row in rows] == [
        *(str(UUID(int=20_000 + index)) for index in range(4)),
        *(str(UUID(int=100_000 + index)) for index in range(6)),
    ]
    assert retained_peak <= 138  # batch + one 128-row scan window
    assert metadata_peak <= 30  # two bounded top-k sets and one quartet
    assert identity_peak <= 13  # batch + a whole quartet
    assert payload_peak <= 4
    record_property("retained_list_peak", retained_peak)
    record_property("metadata_peak_excluding_128_row_driver_window", metadata_peak)
    record_property("identity_map_peak", identity_peak)
    record_property("quartet_payload_peak", payload_peak)


def test_many_bootstrap_quartets_use_one_metadata_scan(factory):
    cycle_id = str(UUID(int=30_001))
    lot_id = str(UUID(int=30_002))
    kinds = (
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.POSITION_LOT_OPENED.value,
        FactKind.TRADE_AUDIT_RECORDED.value,
        FactKind.AUTOMATION_STATE_CHANGED.value,
    )
    payloads = (
        {"position_cycle_id": cycle_id},
        {
            "position_cycle_id": cycle_id,
            "position_lot_id": lot_id,
            "source": "BROKER_POSITION_BOOTSTRAP",
        },
        {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
        {"state": "IN_WORK"},
    )
    with factory.begin() as session:
        for automation_index in range(30):
            automation_id = str(UUID(int=40_000 + automation_index))
            cached = cached_automation(automation_id)
            cached.bootstrap_position_cycle_id = cycle_id
            cached.bootstrap_position_lot_id = lot_id
            session.add(cached)
            base = 50_000 + automation_index * 100
            session.add_all(
                [
                    outbox_row(base + index, automation_id, index + 1, fact_kind=kinds[index], payload=payloads[index])
                    for index in range(4)
                ]
                + [outbox_row(base + 10 + index, automation_id, index + 5) for index in range(12)]
            )

    scans = 0

    def count_scan(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal scans
        if "ORDER BY fact_outbox.automation_id, fact_outbox.sequence_number" in statement:
            scans += 1

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_scan)
    try:
        rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)
    finally:
        event.remove(engine, "before_cursor_execute", count_scan)

    assert scans == 1
    assert len(rows) == 100
    assert [row.event_id for row in rows] == select_old_batch(factory, 100, now=NOW, deadline_ms=0)
    assert all(row.sequence_number <= 4 for row in rows)


def test_many_early_quartets_keep_global_candidate_metadata_bounded(factory):
    cycle_id = str(UUID(int=73_001))
    lot_id = str(UUID(int=73_002))
    single_automation = str(UUID(int=74_999))
    kinds = (
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.POSITION_LOT_OPENED.value,
        FactKind.TRADE_AUDIT_RECORDED.value,
        FactKind.AUTOMATION_STATE_CHANGED.value,
    )
    payloads = (
        {"position_cycle_id": cycle_id},
        {"position_cycle_id": cycle_id, "position_lot_id": lot_id, "source": "BROKER_POSITION_BOOTSTRAP"},
        {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
        {"state": "IN_WORK"},
    )
    with factory.begin() as session:
        session.add(cached_automation(single_automation))
        session.add_all(
            [
                outbox_row(74_000 + index, single_automation, index + 1, occurred_at=NOW + timedelta(days=1))
                for index in range(13)
            ]
        )
        for automation_index in range(6):
            automation_id = str(UUID(int=75_000 + automation_index))
            cached = cached_automation(automation_id)
            cached.bootstrap_position_cycle_id = cycle_id
            cached.bootstrap_position_lot_id = lot_id
            session.add(cached)
            session.add_all(
                [
                    outbox_row(
                        76_000 + automation_index * 4 + index,
                        automation_id,
                        index + 1,
                        fact_kind=kinds[index],
                        payload=payloads[index],
                    )
                    for index in range(4)
                ]
            )

    metadata_peak = 0

    def trace(frame, event, _arg):
        nonlocal metadata_peak
        if event == "line" and frame.f_code.co_name == "ready_fact_outbox":
            local = frame.f_locals
            metadata_peak = max(
                metadata_peak,
                local.get("candidate_rows", 0)
                + local.get("local_rows", 0)
                + len(local.get("unit", ()))
                + len(local.get("selected", ())),
            )
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        rows = LocalAutomationRepository(factory).ready_fact_outbox(10, now=NOW + timedelta(days=2), deadline_ms=0)
    finally:
        sys.settrace(previous)

    assert [row.event_id for row in rows] == select_old_batch(factory, 10, now=NOW + timedelta(days=2), deadline_ms=0)
    assert len(rows) == 12
    assert metadata_peak <= 30


def test_late_quartet_does_not_evict_earlier_peer_from_candidate_batch(factory):
    cycle_id = str(UUID(int=71_001))
    lot_id = str(UUID(int=71_002))
    peer_automation = str(UUID(int=300))
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        session.add(outbox_row(72_300, peer_automation, 1))
        session.add_all([outbox_row(72_200 + index, AUTOMATION_A, index + 1) for index in range(8)])
        session.add_all(
            [
                outbox_row(
                    72_100,
                    AUTOMATION_A,
                    9,
                    fact_kind=FactKind.POSITION_CYCLE_UPDATED.value,
                    payload={"position_cycle_id": cycle_id},
                ),
                outbox_row(
                    72_101,
                    AUTOMATION_A,
                    10,
                    fact_kind=FactKind.POSITION_LOT_OPENED.value,
                    payload={
                        "position_cycle_id": cycle_id,
                        "position_lot_id": lot_id,
                        "source": "BROKER_POSITION_BOOTSTRAP",
                    },
                ),
                outbox_row(72_102, AUTOMATION_A, 11, payload={"stage": "BOOTSTRAP_POSITION_ADOPTED"}),
                outbox_row(
                    72_103,
                    AUTOMATION_A,
                    12,
                    fact_kind=FactKind.AUTOMATION_STATE_CHANGED.value,
                    payload={"state": "IN_WORK"},
                ),
            ]
        )

    rows = LocalAutomationRepository(factory).ready_fact_outbox(5, now=NOW, deadline_ms=0)

    assert [row.event_id for row in rows] == [
        *(str(UUID(int=72_100 + index)) for index in range(4)),
        str(UUID(int=72_300)),
    ]
    assert [row.event_id for row in rows] == select_old_batch(factory, 5, now=NOW, deadline_ms=0)


def test_large_fully_blocked_bootstrap_queue_uses_one_metadata_scan(factory):
    cycle_id = str(UUID(int=81_001))
    lot_id = str(UUID(int=81_002))
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, AUTOMATION_A)
        cached.bootstrap_position_cycle_id = cycle_id
        cached.bootstrap_position_lot_id = lot_id
        session.add_all(
            [
                outbox_row(
                    82_000,
                    AUTOMATION_A,
                    1,
                    fact_kind=FactKind.POSITION_CYCLE_UPDATED.value,
                    payload={"position_cycle_id": cycle_id},
                    next_retry_at=NOW + timedelta(days=1),
                ),
                outbox_row(
                    82_001,
                    AUTOMATION_A,
                    2,
                    fact_kind=FactKind.POSITION_LOT_OPENED.value,
                    payload={
                        "position_cycle_id": cycle_id,
                        "position_lot_id": lot_id,
                        "source": "BROKER_POSITION_BOOTSTRAP",
                    },
                ),
                outbox_row(82_002, AUTOMATION_A, 3, payload={"stage": "BOOTSTRAP_POSITION_ADOPTED"}),
                outbox_row(
                    82_003,
                    AUTOMATION_A,
                    4,
                    fact_kind=FactKind.AUTOMATION_STATE_CHANGED.value,
                    payload={"state": "IN_WORK"},
                ),
                outbox_row(83_000, AUTOMATION_B, 1, delivery_state="FAILED"),
                *(outbox_row(100_000 + index, AUTOMATION_B, index + 2) for index in range(50_000)),
            ]
        )

    scans = 0

    def count_scan(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal scans
        if "ORDER BY fact_outbox.automation_id, fact_outbox.sequence_number" in statement:
            scans += 1

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_scan)
    try:
        rows = LocalAutomationRepository(factory).ready_fact_outbox(100, now=NOW, deadline_ms=0)
    finally:
        event.remove(engine, "before_cursor_execute", count_scan)

    assert rows == []
    assert scans == 1


def test_compare_mode_exits_nonzero_when_current_selector_mismatches(factory, tmp_path, monkeypatch):
    output = tmp_path
    datasets = output / "datasets"
    datasets.mkdir()
    insert_outbox_rows(factory, [outbox_row(728, AUTOMATION_A, 1)])
    expected = select_old_batch(factory, 1, now=NOW, deadline_ms=0)
    factory.kw["bind"].dispose()
    database_path = Path(factory.kw["bind"].url.database)
    with database_path.open("rb") as source, gzip.open(datasets / "small.sqlite.gz", "wb") as target:
        copyfileobj(source, target)
    (output / "selector-baseline-v2.json").write_text(
        json.dumps(
            {
                "scenarios": {
                    "normal": {
                        "1": {
                            "dataset_path": "datasets/small.sqlite.gz",
                            "oracle_old_selector": {"selected_event_ids_in_order": expected},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / "develop" / "benchmarks"))
    benchmark = importlib.import_module("benchmark_fact_outbox_selector")

    with pytest.raises(SystemExit, match="current selector differs from old oracle"):
        benchmark.compare_baseline(
            SimpleNamespace(output=output, sizes=[1], repeats=1),
            selector=lambda *_args, **_kwargs: [],
        )

    comparison = json.loads((output / "selector-comparison-v2.json").read_text(encoding="utf-8"))
    assert comparison["scenarios"]["normal"]["1"]["current_ids_match_old_oracle"] is False


def test_selector_characterizes_equal_timestamp_id_order_and_deadline_boundary(factory):
    ids = [UUID(int=732), UUID(int=731)]
    insert_outbox_rows(
        factory,
        [outbox_row(value.int, AUTOMATION_A, index + 1) for index, value in enumerate(ids)],
    )
    repository = LocalAutomationRepository(factory)

    before = repository.ready_fact_outbox(3, now=NOW + timedelta(milliseconds=99), deadline_ms=100)
    at_boundary = repository.ready_fact_outbox(3, now=NOW + timedelta(milliseconds=100), deadline_ms=100)

    assert before == []
    assert [row.event_id for row in at_boundary] == [str(UUID(int=731)), str(UUID(int=732))]


def test_diagnostic_aggregate_is_one_scalar_select_without_loading_payload(factory):
    with factory.begin() as session:
        for index, state in enumerate(("PENDING", "PENDING", "FAILED"), start=1):
            session.add(
                FactOutboxModel(
                    event_id=str(index),
                    user_broker_id=SCOPE_ID,
                    automation_id=AUTOMATION_A,
                    sequence_number=index,
                    expected_revision=index,
                    fact_kind="TRADE_AUDIT_RECORDED",
                    payload={"large": "x" * 10000},
                    safe_message="synthetic",
                    occurred_at=NOW,
                    created_at=NOW + timedelta(seconds=index),
                    delivery_state=state,
                    retry_count=index,
                )
            )
    statements = []
    engine = factory.kw["bind"]

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = LocalAutomationRepository(factory).outbox_diagnostics()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.observation == "OBSERVED"
    assert (result.pending_count, result.failed_count, result.max_retry_count) == (2, 1, 2)
    assert result.oldest_pending_at == NOW + timedelta(seconds=1)
    assert len(statements) == 1
    assert "payload" not in statements[0].lower()


def test_empty_diagnostic_queue_has_zero_counts_and_no_pending_metadata(factory):
    result = LocalAutomationRepository(factory).outbox_diagnostics()
    assert (result.pending_count, result.failed_count) == (0, 0)
    assert result.oldest_pending_at is None
    assert result.max_retry_count is None
