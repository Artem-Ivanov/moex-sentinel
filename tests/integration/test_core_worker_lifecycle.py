"""Core HTTP authority and Worker SQLite proposals across the normal lifecycle."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.models import AutomationEventModel, PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models import Base as CoreBase
from sentinel_contracts.automation_lifecycle import InvalidAutomationTransition
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    AutomationStateChangedPayload,
    FactEnvelope,
    FactIngressErrorCode,
    FactKind,
    PositionCycleUpdatedEnvelope,
)
from tests.contracts.trading_facts_helpers import all_envelopes
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 9, 9, 12, 0, 0, 123000, tzinfo=UTC)
SCOPE_ID = UUID("00000000-0000-4000-8000-000000000701")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000702")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000703")
AUTOMATION_PATH = f"/api/trading-automations/{AUTOMATION_ID}"
FACT_ADAPTER: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)


@dataclass
class LifecycleContext:
    http: TestClient
    core: CoreClient
    core_factory: sessionmaker[Session]
    worker_factory: sessionmaker[Session]
    worker: LocalAutomationRepository
    sync: FactSynchronizationService

    def assert_core(self, state: str, revision: int, sequence: int, events: int) -> None:
        with self.core_factory() as session:
            model = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
            assert (model.state, model.revision, model.last_sequence_number) == (state, revision, sequence)
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == events

    def assert_worker(self, state: str, revision: int, sequence: int, outbox: int) -> None:
        with self.worker_factory() as session:
            model = session.get_one(CachedAutomationModel, str(AUTOMATION_ID))
            assert (model.state, model.revision, model.last_sequence_number) == (state, revision, sequence)
            assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == outbox

    def propose(self, state: str) -> None:
        self.worker.transition_state(
            automation_id=str(AUTOMATION_ID),
            state=state,
            safe_message="Synthetic lifecycle transition",
            occurred_at=NOW,
        )

    def pending_facts(self) -> list[FactEnvelope]:
        rows = self.worker.ready_fact_outbox(100, now=NOW, deadline_ms=0)
        return [
            FACT_ADAPTER.validate_python(
                {
                    "event_id": row.event_id,
                    "user_broker_id": row.user_broker_id,
                    "automation_id": row.automation_id,
                    "sequence_number": row.sequence_number,
                    "expected_revision": row.expected_revision,
                    "fact_kind": row.fact_kind,
                    "payload": row.payload,
                    "safe_message": row.safe_message,
                    "occurred_at": row.occurred_at,
                }
            )
            for row in rows
        ]

    def reconcile_snapshot(self) -> None:
        statuses = self.core.automation_statuses([AUTOMATION_ID])
        assert statuses.missing_automation_ids == ()
        assert len(statuses.automations) == 1
        status = statuses.automations[0]
        self.worker.reconcile_from_core(
            str(status.automation_id),
            state=status.state.value,
            revision=status.revision,
            last_sequence_number=status.last_sequence_number,
        )


@pytest.fixture
def lifecycle(tmp_path: Path) -> Iterator[LifecycleContext]:
    app = create_app(database_url=f"sqlite:///{tmp_path / 'core.db'}")
    worker_engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    WorkerBase.metadata.create_all(worker_engine)
    worker_factory = sessionmaker(worker_engine, expire_on_commit=False)
    worker = LocalAutomationRepository(worker_factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    try:
        with TestClient(app) as http:
            core_engine = app.state.database_engine
            CoreBase.metadata.create_all(core_engine)
            core_factory = app.state.session_factory
            with core_factory.begin() as session:
                session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
                session.flush()
                session.add(instrument_model(str(INSTRUMENT_ID), str(SCOPE_ID)))
                session.flush()
                session.add(
                    automation_model(
                        str(AUTOMATION_ID),
                        user_broker_id=str(SCOPE_ID),
                        instrument_id=str(INSTRUMENT_ID),
                        state="IN_QUEUE",
                    )
                )
            assert core_engine.url.database != worker_engine.url.database
            assert not inspect(worker_engine).has_table(TradingAutomationModel.__tablename__)
            assert not inspect(core_engine).has_table(CachedAutomationModel.__tablename__)
            core = CoreClient(http)
            sync = FactSynchronizationService(worker, core, now=lambda: NOW, sleep=lambda _: None, deadline_ms=0)
            yield LifecycleContext(http, core, core_factory, worker_factory, worker, sync)
    finally:
        worker_engine.dispose()


def test_claim_activation_retry_and_public_lifecycle_use_core_authority(lifecycle: LifecycleContext) -> None:
    commands = lifecycle.sync.claim_commands("synthetic-worker", 10)
    assert len(commands) == 1
    assert commands[0].automation_id == AUTOMATION_ID
    assert commands[0].state is AutomationState.IN_QUEUE
    assert commands[0].bootstrap is None
    lifecycle.assert_core("IN_QUEUE", 1, 0, 0)
    lifecycle.assert_worker("IN_QUEUE", 1, 0, 0)

    lifecycle.propose("IN_WORK")
    lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("IN_WORK", 2, 1, 1)
    lifecycle.assert_core("IN_QUEUE", 1, 0, 0)
    assert lifecycle.sync.claim_commands("synthetic-worker", 10) == []
    lifecycle.assert_worker("IN_WORK", 2, 1, 1)

    # Core commits, but the Worker has not received the ACK; its durable outbox survives.
    facts = lifecycle.pending_facts()
    first = lifecycle.core.publish_facts(facts)
    assert first.failures == ()
    assert first.results[0].accepted_event_ids == (facts[0].event_id,)
    assert (first.results[0].current_revision, first.results[0].accepted_through_sequence) == (2, 1)
    lifecycle.assert_core("IN_WORK", 2, 1, 1)
    lifecycle.assert_worker("IN_WORK", 2, 1, 1)
    assert lifecycle.sync.flush_outbox() is True
    lifecycle.assert_core("IN_WORK", 2, 1, 1)
    lifecycle.assert_worker("IN_WORK", 2, 1, 0)
    assert lifecycle.core.publish_facts(facts) == first

    held = lifecycle.http.post(f"{AUTOMATION_PATH}/hold")
    assert held.status_code == 200
    assert (held.json()["state"], held.json()["revision"]) == ("HOLD", 3)
    lifecycle.assert_core("HOLD", 3, 1, 1)
    lifecycle.assert_worker("IN_WORK", 2, 1, 0)
    lifecycle.reconcile_snapshot()
    lifecycle.assert_worker("HOLD", 3, 1, 0)
    with pytest.raises(InvalidAutomationTransition):
        lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("HOLD", 3, 1, 0)

    resumed = lifecycle.http.post(f"{AUTOMATION_PATH}/resume")
    assert resumed.status_code == 200
    assert (resumed.json()["state"], resumed.json()["revision"]) == ("IN_QUEUE", 4)
    lifecycle.assert_core("IN_QUEUE", 4, 1, 1)
    lifecycle.assert_worker("HOLD", 3, 1, 0)
    resumed_commands = lifecycle.sync.claim_commands("synthetic-worker", 10)
    assert len(resumed_commands) == 1
    assert resumed_commands[0].state is AutomationState.IN_QUEUE
    lifecycle.assert_worker("IN_QUEUE", 4, 1, 0)
    lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("IN_WORK", 5, 2, 1)
    lifecycle.assert_core("IN_QUEUE", 4, 1, 1)
    assert lifecycle.sync.flush_outbox() is True
    lifecycle.assert_core("IN_WORK", 5, 2, 2)
    lifecycle.assert_worker("IN_WORK", 5, 2, 0)

    closed = lifecycle.http.post(f"{AUTOMATION_PATH}/close")
    assert closed.status_code == 200
    assert (closed.json()["state"], closed.json()["revision"]) == ("CLOSED", 6)
    lifecycle.reconcile_snapshot()
    lifecycle.assert_core("CLOSED", 6, 2, 2)
    lifecycle.assert_worker("CLOSED", 6, 2, 0)
    with pytest.raises(InvalidAutomationTransition):
        lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("CLOSED", 6, 2, 0)
    assert lifecycle.http.post(f"{AUTOMATION_PATH}/resume").status_code == 409
    assert lifecycle.sync.claim_commands("synthetic-worker", 10) == []


@pytest.mark.parametrize("public_action", ["hold", "close"])
def test_invalid_http_fact_group_rolls_back_supporting_fact(lifecycle: LifecycleContext, public_action: str) -> None:
    assert len(lifecycle.sync.claim_commands("synthetic-worker", 10)) == 1
    response = lifecycle.http.post(f"{AUTOMATION_PATH}/{public_action}")
    assert response.status_code == 200
    expected_state = "HOLD" if public_action == "hold" else "CLOSED"
    lifecycle.reconcile_snapshot()
    cycle = next(value for value in all_envelopes() if isinstance(value, PositionCycleUpdatedEnvelope))
    cycle = cycle.model_copy(
        update={
            "user_broker_id": SCOPE_ID,
            "automation_id": AUTOMATION_ID,
            "sequence_number": 1,
            "expected_revision": 2,
            "occurred_at": NOW,
            "payload": cycle.payload.model_copy(update={"instrument_id": INSTRUMENT_ID}),
        }
    )
    activation = AutomationStateChangedEnvelope(
        event_id=UUID("00000000-0000-4000-8000-000000000704"),
        user_broker_id=SCOPE_ID,
        automation_id=AUTOMATION_ID,
        sequence_number=2,
        expected_revision=2,
        fact_kind=FactKind.AUTOMATION_STATE_CHANGED,
        payload=AutomationStateChangedPayload(
            state=AutomationState.IN_WORK,
            suspended_from_state=None,
            hold_reason=None,
            closed_at=None,
        ),
        safe_message="Synthetic forbidden activation",
        occurred_at=NOW,
    )

    result = lifecycle.core.publish_facts([cycle, activation])

    assert result.results == ()
    assert len(result.failures) == 1
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    assert result.failures[0].retryable is False
    assert result.failures[0].event_ids == (cycle.event_id, activation.event_id)
    lifecycle.assert_core(expected_state, 2, 0, 0)
    lifecycle.assert_worker(expected_state, 2, 0, 0)
    with lifecycle.core_factory() as session:
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 0

    # The supporting fact is valid independently: only the later activation rejects the group.
    accepted = lifecycle.core.publish_facts([cycle])
    assert accepted.failures == ()
    assert accepted.results[0].accepted_event_ids == (cycle.event_id,)
    assert (accepted.results[0].current_revision, accepted.results[0].accepted_through_sequence) == (2, 1)
    lifecycle.assert_core(expected_state, 2, 1, 1)
    with lifecycle.core_factory() as session:
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 1


def test_rejected_stale_outbox_keeps_authoritative_closed_snapshot(lifecycle: LifecycleContext) -> None:
    assert len(lifecycle.sync.claim_commands("synthetic-worker", 10)) == 1
    lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("IN_WORK", 2, 1, 1)
    assert lifecycle.http.post(f"{AUTOMATION_PATH}/close").status_code == 200
    lifecycle.assert_core("CLOSED", 2, 0, 0)

    assert lifecycle.sync.flush_outbox() is True

    lifecycle.assert_core("CLOSED", 2, 0, 0)
    lifecycle.assert_worker("CLOSED", 2, 0, 1)
    with lifecycle.worker_factory() as session:
        failed = session.scalars(select(FactOutboxModel)).one()
        assert failed.delivery_state == "FAILED"
    assert lifecycle.pending_facts() == []
    with pytest.raises(InvalidAutomationTransition):
        lifecycle.propose("IN_WORK")
    lifecycle.assert_worker("CLOSED", 2, 0, 1)
