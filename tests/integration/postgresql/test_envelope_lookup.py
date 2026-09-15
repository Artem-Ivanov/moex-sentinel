"""Concurrent production ingress against an isolated migrated PostgreSQL schema."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import AutomationEnvelopeDraft
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import AutomationEventModel, TradeAuditEventModel, TradingAutomationModel
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository
from sentinel_contracts.trading_facts import (
    FactBatchResult,
    FactEnvelope,
    FactIngressErrorCode,
    TradeAuditRecordedEnvelope,
)
from tests.contracts.trading_facts_helpers import all_envelopes
from tests.services.test_trading_fact_ingress import AUTOMATION_A, INSTRUMENT_A, SCOPE_ID, service, state_fact
from tests.storage.test_envelope_lookup import TestEnvelopeLookup as _LookupContract
from tests.storage.test_envelope_lookup import envelope_session as _shared_envelope_session
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model

pytestmark = pytest.mark.postgresql
envelope_session = _shared_envelope_session


@pytest.fixture
def envelope_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


class TestPostgresqlEnvelopeLookup(_LookupContract):
    """The same lookup and SQL-budget contract on migrated PostgreSQL."""


@pytest.fixture
def ingress_factory(isolated_postgresql_database_url: URL) -> Iterator[sessionmaker[Session]]:
    options = str(isolated_postgresql_database_url.query.get("options", ""))
    bounded_url = isolated_postgresql_database_url.update_query_dict(
        {"options": f"{options} -cstatement_timeout=8000 -clock_timeout=5000"}
    )
    engine = create_database_engine(bounded_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            session.add(user_broker_model(str(SCOPE_ID), "synthetic-account"))
            session.flush()
            instrument = instrument_model(str(INSTRUMENT_A), str(SCOPE_ID))
            instrument.ticker = "SYNTH"
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(str(AUTOMATION_A), user_broker_id=str(SCOPE_ID), instrument_id=str(INSTRUMENT_A))
            )
        yield factory
    finally:
        engine.dispose()


def concurrent_publish(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, facts: tuple[FactEnvelope, FactEnvelope]
) -> list[FactBatchResult]:
    """Both real lookups finish before either transaction applies its fact."""
    readers = Barrier(2)
    original_lookup = TradingAuditRepository.find_envelope_matches

    def synchronized_lookup(
        repository: TradingAuditRepository,
        user_broker_id: str,
        event_id: str,
        automation_id: str,
        sequence_number: int,
    ) -> tuple[AutomationEnvelopeDraft, ...]:
        matches = original_lookup(repository, user_broker_id, event_id, automation_id, sequence_number)
        assert matches == ()
        readers.wait(timeout=10)
        return matches

    with monkeypatch.context() as patch:
        patch.setattr(TradingAuditRepository, "find_envelope_matches", synchronized_lookup)
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                futures = [pool.submit(service(factory).publish, [fact]) for fact in facts]
                return [future.result(timeout=20) for future in futures]
            finally:
                readers.abort()


def test_concurrent_exact_delivery_commits_once_and_accepts_retry(
    ingress_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fact = state_fact(AUTOMATION_A, event_id=uuid4())

    outcomes = concurrent_publish(ingress_factory, monkeypatch, (fact, fact))

    winners = [outcome for outcome in outcomes if outcome.results]
    losers = [outcome for outcome in outcomes if outcome.failures]
    assert len(winners) == len(losers) == 1
    assert winners[0].failures == ()
    assert losers[0].results == ()
    assert len(losers[0].failures) == 1
    # Both ingress reads missed the event. If append_idempotent also reads before
    # the winner commits, its INSERT hits the primary key (untranslated: temporary).
    # If it reads after commit, the fixed received_at makes the existing row exact;
    # the subsequent CAS then detects the winner's changed revision.
    failure = losers[0].failures[0]
    assert failure.code in {
        FactIngressErrorCode.TEMPORARY_CORE_FAILURE,
        FactIngressErrorCode.AUTOMATION_REVISION_CONFLICT,
    }
    assert failure.retryable is (failure.code is FactIngressErrorCode.TEMPORARY_CORE_FAILURE)

    retry = service(ingress_factory).publish([fact])

    assert retry.failures == ()
    assert retry.results == winners[0].results
    assert retry.results[0].accepted_event_ids == (fact.event_id,)
    with ingress_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("HOLD", 2, 1)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 1


def audit_fact(event_id: UUID, audit_event_id: UUID) -> TradeAuditRecordedEnvelope:
    template = next(fact for fact in all_envelopes() if isinstance(fact, TradeAuditRecordedEnvelope))
    return template.model_copy(
        update={
            "event_id": event_id,
            "user_broker_id": SCOPE_ID,
            "automation_id": AUTOMATION_A,
            "sequence_number": 1,
            "expected_revision": 1,
            "payload": template.payload.model_copy(
                update={
                    "audit_event_id": audit_event_id,
                    "instrument_id": INSTRUMENT_A,
                    "decision_id": None,
                    "broker_order_id": None,
                    "execution_id": None,
                }
            ),
        }
    )


def test_concurrent_sequence_collision_rolls_back_payload_and_allows_corrected_delivery(
    ingress_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = (audit_fact(uuid4(), uuid4()), audit_fact(uuid4(), uuid4()))

    outcomes = concurrent_publish(ingress_factory, monkeypatch, facts)

    assert sum(bool(outcome.results) for outcome in outcomes) == 1
    assert sum(bool(outcome.failures) for outcome in outcomes) == 1
    winner_index = next(index for index, outcome in enumerate(outcomes) if outcome.results)
    winner, loser = facts[winner_index], facts[1 - winner_index]
    winning_result, losing_result = outcomes[winner_index], outcomes[1 - winner_index]
    assert winning_result.failures == ()
    assert losing_result.results == ()
    assert len(losing_result.failures) == 1
    assert losing_result.failures[0].code is FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT
    assert losing_result.failures[0].retryable is False
    with ingress_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 1, 1)
        assert list(session.scalars(select(AutomationEventModel.event_id))) == [str(winner.event_id)]
        # The losing audit payload was flushed before its envelope hit the sequence constraint.
        assert list(session.scalars(select(TradeAuditEventModel.event_id))) == [str(winner.payload.audit_event_id)]

    assert service(ingress_factory).publish([winner]).results == winning_result.results
    retry = service(ingress_factory).publish([loser])
    assert retry.results == ()
    assert retry.failures[0].code is FactIngressErrorCode.AUTOMATION_SEQUENCE_CONFLICT

    corrected = loser.model_copy(update={"sequence_number": 2})
    accepted = service(ingress_factory).publish([corrected])
    assert accepted.failures == ()
    assert accepted.results[0].accepted_event_ids == (loser.event_id,)
    assert accepted.results[0].accepted_through_sequence == 2
    with ingress_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_A))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 1, 2)
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 2
        assert session.scalar(select(func.count()).select_from(TradeAuditEventModel)) == 2
