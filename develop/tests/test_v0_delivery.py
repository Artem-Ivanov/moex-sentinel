"""V0 checks through Worker SQLite, real HTTP, and isolated Core PostgreSQL."""

import os
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from develop.benchmarks.pg_metrics import Profile
from develop.benchmarks.pg_server import CoreServer
from develop.benchmarks.postgresql import isolated_database
from moex_sentinel.storage.models import (
    AutomationEventModel,
    PositionCycleModel,
    PositionLotModel,
    TradingAutomationModel,
)
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from moex_sentinel.usecases.trading_fact_ingress import ViewAutomationStatusesUsecase
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import FactBatchResult, FactGroupFailure, FactIngressErrorCode
from tests.integration.test_open_position_bootstrap import (
    AUTOMATION_ID,
    CYCLE_ID,
    INSTRUMENT_ID,
    LOT_ID,
    NOW,
    SCOPE_ID,
    bootstrap_context,
)
from tests.storage.trading_facts_helpers import instrument_model
from tests.trading_automaton.command_factory import command
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.models import FactOutboxModel


@pytest.mark.postgresql
def test_bootstrap_quartet_is_atomic_over_http_and_postgresql(tmp_path):
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    with isolated_database(database_url) as engine:
        core_factory, worker, ingress, _facts = bootstrap_context(
            core_engine=engine, worker_db_path=tmp_path / "worker.db"
        )
        server = CoreServer(ingress, Profile())
        server.start()
        try:
            with httpx.Client(base_url=server.url, trust_env=False) as http:
                synchronizer = FactSynchronizationService(
                    worker, CoreClient(http), now=lambda: NOW, sleep=lambda _: None, batch_size=2, deadline_ms=0
                )
                assert synchronizer.flush_outbox()
                assert worker.ready_fact_outbox(10, now=NOW, deadline_ms=0) == []
        finally:
            server.close()
        with core_factory() as session:
            automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
            assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 2, 4)
            assert session.get_one(PositionCycleModel, str(CYCLE_ID)).quantity_lots == 2
            assert session.get_one(PositionLotModel, str(LOT_ID)).source == "BROKER_POSITION_BOOTSTRAP"
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4


@pytest.mark.postgresql
def test_mixed_http_response_acks_bootstrap_and_keeps_failed_peer(tmp_path):
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    with isolated_database(database_url) as engine:
        core_factory, worker, ingress, _facts = bootstrap_context(
            core_engine=engine, worker_db_path=tmp_path / "worker.db"
        )
        missing = command(automation="missing", broker="missing", instrument="external-instrument").model_copy(
            update={"user_broker_id": SCOPE_ID, "broker_id": SCOPE_ID, "instrument_id": INSTRUMENT_ID}
        )
        assert worker.cache_command(missing)
        worker.transition_state(
            automation_id=str(missing.automation_id),
            state=AutomationState.HOLD.value,
            safe_message="Missing Core automation",
            occurred_at=NOW + timedelta(milliseconds=1),
        )
        server = CoreServer(
            ingress,
            Profile(),
            require_all=False,
            statuses=ViewAutomationStatusesUsecase(AutomationCommandRepository(core_factory)),
        )
        server.start()
        try:
            with httpx.Client(base_url=server.url, trust_env=False) as http:
                synchronizer = FactSynchronizationService(
                    worker, CoreClient(http), now=lambda: NOW, sleep=lambda _: None, batch_size=5, deadline_ms=0
                )
                assert synchronizer.flush_outbox()
        finally:
            server.close()
        with worker._factory() as session:
            remaining = session.scalars(select(FactOutboxModel)).all()
            assert [(row.automation_id, row.sequence_number, row.delivery_state) for row in remaining] == [
                (str(missing.automation_id), 1, "FAILED")
            ]
        with core_factory() as session:
            automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
            assert (automation.revision, automation.last_sequence_number) == (2, 4)
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4


@pytest.mark.postgresql
def test_retryable_mixed_http_response_acks_bootstrap_then_retries_peer(tmp_path):
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    with isolated_database(database_url) as engine:
        core_factory, worker, ingress, _facts = bootstrap_context(
            core_engine=engine, worker_db_path=tmp_path / "worker.db"
        )
        peer_instrument_id = UUID(int=606)
        peer = command(automation="peer", broker="peer", instrument="peer-instrument").model_copy(
            update={"user_broker_id": SCOPE_ID, "broker_id": SCOPE_ID, "instrument_id": peer_instrument_id}
        )
        with core_factory.begin() as session:
            instrument = instrument_model(str(peer_instrument_id), str(SCOPE_ID))
            instrument.external_instrument_id = "peer-instrument"
            instrument.ticker = "PEER"
            instrument.name = "Peer"
            session.add(instrument)
            session.flush()
            session.add(
                TradingAutomationModel(
                    id=str(peer.automation_id),
                    user_broker_id=str(SCOPE_ID),
                    instrument_id=str(peer_instrument_id),
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        assert worker.cache_command(peer)
        worker.transition_state(
            automation_id=str(peer.automation_id),
            state=AutomationState.HOLD.value,
            safe_message="Retryable peer",
            occurred_at=NOW + timedelta(milliseconds=1),
        )

        class FailPeerOnce:
            def __init__(self):
                self.failed = False

            def publish(self, facts):
                if self.failed:
                    return ingress.publish(facts)
                self.failed = True
                bootstrap = [fact for fact in facts if fact.automation_id == AUTOMATION_ID]
                delayed = [fact for fact in facts if fact.automation_id == peer.automation_id]
                assert len(bootstrap) == 4
                assert len(delayed) == 1
                accepted = ingress.publish(bootstrap)
                return FactBatchResult(
                    results=accepted.results,
                    failures=(
                        FactGroupFailure(
                            automation_id=peer.automation_id,
                            code=FactIngressErrorCode.TEMPORARY_CORE_FAILURE,
                            event_ids=(delayed[0].event_id,),
                            sequence_numbers=(1,),
                            retryable=True,
                        ),
                    ),
                )

        server = CoreServer(FailPeerOnce(), Profile(), require_all=False)
        server.start()
        clock = [NOW]
        try:
            with httpx.Client(base_url=server.url, trust_env=False) as http:
                synchronizer = FactSynchronizationService(
                    worker, CoreClient(http), now=lambda: clock[0], sleep=lambda _: None, batch_size=5, deadline_ms=0
                )
                assert synchronizer.flush_outbox()
                with worker._factory() as session:
                    pending = session.scalars(select(FactOutboxModel)).all()
                    assert [
                        (row.automation_id, row.sequence_number, row.delivery_state, row.retry_count, row.next_retry_at)
                        for row in pending
                    ] == [(str(peer.automation_id), 1, "PENDING", 1, NOW + timedelta(seconds=1))]
                with core_factory() as session:
                    assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4
                assert synchronizer.flush_outbox()
                with worker._factory() as session:
                    pending = session.scalars(select(FactOutboxModel)).all()
                    assert [(row.automation_id, row.next_retry_at) for row in pending] == [
                        (str(peer.automation_id), NOW + timedelta(seconds=1))
                    ]
                with core_factory() as session:
                    assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4
                clock[0] += timedelta(seconds=1)
                assert synchronizer.flush_outbox()
        finally:
            server.close()
        with worker._factory() as session:
            assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0
        with core_factory() as session:
            assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 5
            assert session.get_one(TradingAutomationModel, str(peer.automation_id)).last_sequence_number == 1
