"""Successful lease checkpoints survive restarts without slowing owner heartbeats."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionEventModel,
    WorkerRuntimeInstanceModel,
)
from moex_sentinel.storage.repositories.trading_admission import AdmissionConflictError, TradingAdmissionRepository
from sentinel_contracts.trading_admission import OwnerStoppedRequest, RuntimeRegistrationRequest
from tests.storage.trading_facts_helpers import user_broker_model

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000001")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
LEASE = timedelta(seconds=600)


def registration(broker_id=BROKER_ID, runtime_id=RUNTIME_ID):
    return RuntimeRegistrationRequest(
        worker_id="lease-audit-worker",
        runtime_instance_id=runtime_id,
        broker_id=broker_id,
        application_environment="PROD",
        access_mode="READ_ONLY",
        strategy_enabled=False,
    )


@pytest.fixture
def lease_store(tmp_path):
    url = f"sqlite:///{tmp_path / 'lease-audit.db'}"
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        broker = user_broker_model(str(BROKER_ID), "synthetic-account")
        broker.environment = "PROD"
        session.add(broker)
    repository = TradingAdmissionRepository(factory)
    repository.register_runtime(registration(), secret_digest="d" * 64, now=NOW, lease_expires_at=NOW + LEASE)
    try:
        yield engine, factory, repository, url
    finally:
        engine.dispose()


def renew(repository, seconds, *, runtime_id=RUNTIME_ID, generation=1):
    return repository.renew_owner(
        str(runtime_id), generation, final_clock=lambda: NOW + timedelta(seconds=seconds), lease_duration=LEASE
    )


def checkpoints(factory, broker_id=BROKER_ID):
    with factory() as session:
        return list(
            session.scalars(
                select(TradingAdmissionEventModel)
                .where(
                    TradingAdmissionEventModel.user_broker_id == str(broker_id),
                    TradingAdmissionEventModel.event_type == "OWNER_LEASE_RENEWED",
                )
                .order_by(TradingAdmissionEventModel.occurred_at)
            )
        )


def test_lease_and_last_seen_update_each_heartbeat_but_audit_only_every_300_seconds(lease_store):
    _engine, factory, repository, _url = lease_store
    for seconds in (0, 3, 299, 300):
        now = NOW + timedelta(seconds=seconds)
        result = renew(repository, seconds)
        assert result.lease_expires_at == now + LEASE
        with factory() as session:
            runtime = session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID))
            admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
            assert runtime.last_seen_at == now
            assert runtime.lease_expires_at == admission.lease_expires_at == now + LEASE
            assert admission.revision == 1
    events = checkpoints(factory)
    assert [event.occurred_at for event in events] == [NOW, NOW + timedelta(seconds=300)]
    assert [(event.runtime_instance_id, event.generation) for event in events] == [(str(RUNTIME_ID), 1)] * 2


def test_reopened_database_keeps_last_successful_checkpoint(lease_store):
    engine, factory, repository, url = lease_store
    renew(repository, 0)
    engine.dispose()
    reopened = create_database_engine(url)
    try:
        factory = create_session_factory(reopened)
        repository = TradingAdmissionRepository(factory)
        renew(repository, 299)
        assert len(checkpoints(factory)) == 1
        renew(repository, 300)
        assert len(checkpoints(factory)) == 2
    finally:
        reopened.dispose()


@pytest.mark.parametrize("structured_generation", [None, 1])
def test_legacy_payload_checkpoint_and_structured_generation_take_precedence(lease_store, structured_generation):
    _engine, factory, repository, _url = lease_store
    with factory.begin() as session:
        session.add(
            TradingAdmissionEventModel(
                event_id=str(uuid4()),
                user_broker_id=str(BROKER_ID),
                event_type="OWNER_LEASE_RENEWED",
                occurred_at=NOW,
                generation=structured_generation,
                payload={"generation": 1 if structured_generation is None else 99},
            )
        )
        session.add(
            TradingAdmissionEventModel(
                event_id=str(uuid4()),
                user_broker_id=str(BROKER_ID),
                event_type="SUBMIT_DENIED",
                occurred_at=NOW + timedelta(seconds=299),
                payload={},
            )
        )
    renew(repository, 299)
    assert len(checkpoints(factory)) == 1
    renew(repository, 300)
    assert len(checkpoints(factory)) == 2


@pytest.mark.parametrize("legacy_checkpoint", [False, True])
def test_new_generation_has_immediate_checkpoint_even_at_same_timestamp(lease_store, legacy_checkpoint):
    _engine, factory, repository, _url = lease_store
    renew(repository, 0)
    candidate_id = UUID("00000000-0000-4000-8000-000000000003")
    repository.register_runtime(
        registration(runtime_id=candidate_id),
        secret_digest="d" * 64,
        now=NOW,
        lease_expires_at=NOW + LEASE,
    )
    service = TradingAdmissionService(repository, clock=lambda: NOW, owner_lease_seconds=600)
    transferred = service.owner_stopped(
        str(BROKER_ID),
        OwnerStoppedRequest(
            expected_revision=1,
            old_runtime_instance_id=RUNTIME_ID,
            old_generation=1,
            candidate_runtime_instance_id=candidate_id,
            stopped_at=NOW,
            auto_restart_disabled=True,
            evidence_ref="lease-audit-owner-stopped",
        ),
        actor="operator",
    )
    assert transferred.owner_generation == 2
    renew(repository, 0, runtime_id=candidate_id, generation=2)
    if legacy_checkpoint:
        with factory.begin() as session:
            event = session.scalar(
                select(TradingAdmissionEventModel).where(
                    TradingAdmissionEventModel.event_type == "OWNER_LEASE_RENEWED",
                    TradingAdmissionEventModel.payload["generation"].as_integer() == 2,
                )
            )
            event.runtime_instance_id = None
            event.generation = None
    renew(repository, 0, runtime_id=candidate_id, generation=2)
    assert sorted(event.payload["generation"] for event in checkpoints(factory)) == [1, 2]


def test_accounts_have_independent_checkpoints(lease_store):
    _engine, factory, repository, _url = lease_store
    other_broker = UUID("00000000-0000-4000-8000-000000000004")
    other_runtime = UUID("00000000-0000-4000-8000-000000000005")
    with factory.begin() as session:
        broker = user_broker_model(str(other_broker), "other-synthetic-account")
        broker.environment = "PROD"
        session.add(broker)
    repository.register_runtime(
        registration(other_broker, other_runtime),
        secret_digest="d" * 64,
        now=NOW,
        lease_expires_at=NOW + LEASE,
    )
    renew(repository, 0)
    renew(repository, 3, runtime_id=other_runtime)
    renew(repository, 299)
    renew(repository, 300, runtime_id=other_runtime)
    assert [event.occurred_at for event in checkpoints(factory)] == [NOW]
    assert [event.occurred_at for event in checkpoints(factory, other_broker)] == [NOW + timedelta(seconds=3)]
    renew(repository, 303, runtime_id=other_runtime)
    assert len(checkpoints(factory, other_broker)) == 2


def test_suppressed_success_does_not_suppress_expiry_or_allow_wrong_generation(lease_store):
    _engine, factory, repository, _url = lease_store
    renew(repository, 0)
    with pytest.raises(AdmissionConflictError, match="current account owner"):
        renew(repository, 3, generation=2)
    with factory() as session:
        assert session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID)).last_seen_at == NOW
    with pytest.raises(AdmissionConflictError, match="Expired owner lease"):
        renew(repository, 600)
    with factory() as session:
        admission = session.get(TradingAccountAdmissionModel, str(BROKER_ID))
        assert admission.admission_status == "DISARMED"
        assert admission.revision == 2
        assert session.get(WorkerRuntimeInstanceModel, str(RUNTIME_ID)).last_seen_at == NOW
        expired = list(
            session.scalars(
                select(TradingAdmissionEventModel).where(TradingAdmissionEventModel.event_type == "OWNER_LEASE_EXPIRED")
            )
        )
        assert len(expired) == 1
        assert expired[0].occurred_at == NOW + timedelta(seconds=600)
    assert len(checkpoints(factory)) == 1
