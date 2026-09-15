"""Baseline trading observability repository tests."""

from typing import TypeVar

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    TradeAuditEventDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import automation_model, seed_buy_and_sell_executions

FactDraftT = TypeVar("FactDraftT", AutomationEnvelopeDraft, TradeAuditEventDraft)


@pytest.fixture
def database(core_database: tuple[Engine, Session]) -> tuple[Engine, Session]:
    """Seed the BUY/SELL history used by this repository's scenarios."""
    _, session = core_database
    seed_buy_and_sell_executions(session)
    return core_database


def fact_value(value_type: type[FactDraftT]) -> FactDraftT:
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


def test_audit_and_envelope_accept_exact_retry(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAuditRepository(session)
    audit = fact_value(TradeAuditEventDraft).model_copy(update={"execution_id": "execution-buy"})
    envelope = fact_value(AutomationEnvelopeDraft)

    assert repository.append_audit("scope-1", audit) == audit
    assert repository.append_audit("scope-1", audit.model_copy(deep=True)) == audit
    assert repository.append_envelope("scope-1", envelope) == envelope
    assert repository.append_envelope("scope-1", envelope.model_copy(deep=True)) == envelope


def test_repeated_sequence_with_new_event_is_rejected(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAuditRepository(session)
    envelope = fact_value(AutomationEnvelopeDraft)
    repository.append_envelope("scope-1", envelope)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_envelope("scope-1", envelope.model_copy(update={"event_id": "envelope-2"}))

    assert caught.value.code is TradingFactErrorCode.SEQUENCE_CONFLICT


def test_conflicting_audit_event_does_not_expose_payload(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAuditRepository(session)
    audit = fact_value(TradeAuditEventDraft).model_copy(update={"execution_id": "execution-buy"})
    repository.append_audit("scope-1", audit)
    conflict = audit.model_copy(update={"safe_message": "synthetic-payload", "data": {"payload": "synthetic-payload"}})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_audit("scope-1", conflict)

    assert caught.value.code is TradingFactErrorCode.FACT_ID_CONFLICT
    assert "synthetic-payload" not in str(caught.value)


def test_observability_repository_rejects_cross_scope_value(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAuditRepository(session)
    envelope = fact_value(AutomationEnvelopeDraft).model_copy(update={"user_broker_id": "scope-2"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_envelope("scope-1", envelope)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE


def test_audit_rejects_same_scope_cross_automation_lineage(database: tuple[Engine, Session]) -> None:
    _, session = database
    session.add(automation_model("automation-2", state="CLOSED", closed=True))
    session.flush()
    repository = TradingAuditRepository(session)
    audit = fact_value(TradeAuditEventDraft).model_copy(
        update={"automation_id": "automation-2", "execution_id": "execution-buy"}
    )

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_audit("scope-1", audit)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
