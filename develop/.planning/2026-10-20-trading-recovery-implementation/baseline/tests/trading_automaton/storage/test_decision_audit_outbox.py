"""Decision summaries retain the unified outbox durability contract."""

from datetime import timedelta
from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_facts import FactKind
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import (
    AUDIT_ID,
    AUDIT_PROCESS_ID,
    NOW,
    baseline_command,
    filled_buy,
)
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.storage.database import create_worker_engine, initialize_worker_schema
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import BusinessAuditEventModel, FactOutboxModel
from trading_automaton.storage.repository import LocalAutomationRepository


@pytest.mark.parametrize("kind", ["WAIT", "BUY_MORE", "SELL_PART", "SELL_ALL"])
def test_summary_outbox_retry_restart_ack_and_following_order_facts(tmp_path, kind):
    database_url = f"sqlite:///{tmp_path / 'decision-audit.sqlite'}"
    engine = create_worker_engine(database_url)
    initialize_worker_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    command = baseline_command()
    automation_id = str(command.automation_id)
    repo.cache_command(command)
    values = {
        "process_id": AUDIT_PROCESS_ID,
        "automation_id": automation_id,
        "broker_id": str(command.broker_id),
        "account_id": command.account_id,
        "instrument_id": command.external_instrument_id,
        "decision": kind,
        "reason_code": "TEST",
        "cycle_pending_low_before": "100",
        "cycle_pending_low_after": "99",
        "token": "unsafe",
    }

    def record_summary():
        identifiers = count(UUID(AUDIT_ID).int)
        BusinessAuditService(
            repo, now=lambda: NOW, id_factory=lambda: str(UUID(int=next(identifiers)))
        ).record_decision_process(**values)

    try:
        record_summary()
        first = repo.ready_fact_outbox(100, now=NOW, deadline_ms=0)
        assert len(first) == 1
        assert first[0].fact_kind == FactKind.TRADE_AUDIT_RECORDED.value
        assert first[0].sequence_number == 1
        assert first[0].payload["stage"] == BusinessAuditStage.STRATEGY_DECISION_MADE.value
        assert first[0].payload["data"] == {
            "decision": kind,
            "reason_code": "TEST",
            "cycle_pending_low_before": "100",
            "cycle_pending_low_after": "99",
        }
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 1
            assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 1

        record_summary()
        assert repo.ready_fact_outbox(100, now=NOW, deadline_ms=0) == first
        retry_at = NOW + timedelta(seconds=5)
        repo.schedule_fact_retry((first[0].event_id,), retry_count=1, next_retry_at=retry_at)
        assert repo.ready_fact_outbox(100, now=NOW, deadline_ms=0) == []
        retried = repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0)
        assert len(retried) == 1
        assert retried[0].retry_count == 1
        assert retried[0].model_dump(exclude={"retry_count", "next_retry_at", "updated_at"}) == first[0].model_dump(
            exclude={"retry_count", "next_retry_at", "updated_at"}
        )
    finally:
        engine.dispose()

    engine = create_worker_engine(database_url)
    initialize_worker_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    try:
        assert repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0) == retried
        repo.save_decision_batch((decision_item(command),), occurred_at=retry_at)
        facts = repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0)
        assert [fact.sequence_number for fact in facts] == [1, 2, 3]
        assert [fact.fact_kind for fact in facts] == [
            FactKind.TRADE_AUDIT_RECORDED.value,
            FactKind.TRADE_DECISION_RECORDED.value,
            FactKind.BROKER_ORDER_RECORDED.value,
        ]
        repo.acknowledge_fact_outbox(automation_id, accepted_through_sequence=1, current_revision=1)
        assert repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0) == facts[1:]
        record_summary()
        assert repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0) == facts[1:]
        repo.finalize_execution(filled_buy())
        final_facts = repo.ready_fact_outbox(100, now=retry_at, deadline_ms=0)
        assert [fact.sequence_number for fact in final_facts] == list(range(2, len(final_facts) + 2))
        assert FactKind.TRADE_EXECUTION_RECORDED.value in [fact.fact_kind for fact in final_facts]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 1
    finally:
        engine.dispose()
