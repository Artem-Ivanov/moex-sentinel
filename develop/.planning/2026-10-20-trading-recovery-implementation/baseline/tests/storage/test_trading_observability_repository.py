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
from tests.storage.trading_facts_helpers import NOW, automation_model, seed_buy_and_sell_executions

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


def test_recent_decision_reasons_are_scoped_bounded_and_safe(database: tuple[Engine, Session]) -> None:
    _, session = database
    session.add(automation_model("automation-2", state="CLOSED", closed=True))
    session.flush()
    repository = TradingAuditRepository(session)
    audit = fact_value(TradeAuditEventDraft)
    for event_id, stage, automation_id, occurred_at, data in (
        (
            "old",
            "STRATEGY_DECISION_MADE",
            "automation-1",
            NOW,
            {"decision": "OLD", "reason_code": "OLD_REASON"},
        ),
        (
            "new",
            "STRATEGY_DECISION_MADE",
            "automation-1",
            NOW.replace(minute=1),
            {"decision": "WAIT", "reason_code": "NO_SIGNAL", "secret": "hidden"},
        ),
        (
            "other-stage",
            "BROKER_ORDER_SUBMITTED",
            "automation-1",
            NOW.replace(minute=2),
            {"decision": "BUY", "reason_code": "ORDER"},
        ),
        (
            "other-automation",
            "STRATEGY_DECISION_MADE",
            "automation-2",
            NOW.replace(minute=3),
            {"decision": "SELL", "reason_code": "OTHER"},
        ),
        (
            "malformed",
            "STRATEGY_DECISION_MADE",
            "automation-1",
            NOW.replace(minute=4),
            {"decision": {"nested": "secret"}, "reason_code": ["secret"]},
        ),
        (
            "long",
            "STRATEGY_DECISION_MADE",
            "automation-1",
            NOW.replace(minute=5),
            {"decision": "x" * 129, "reason_code": "R"},
        ),
    ):
        repository.append_audit(
            "scope-1",
            audit.model_copy(
                update={
                    "event_id": event_id,
                    "stage": stage,
                    "automation_id": automation_id,
                    "occurred_at": occurred_at,
                    "data": data,
                    "decision_id": None,
                    "broker_order_id": None,
                    "execution_id": None,
                }
            ),
        )

    results = repository.list_recent_decision_reasons("scope-1", "automation-1", 4)

    assert [item.event_id for item in results] == ["long", "malformed", "new", "old"]
    assert (results[0].decision, results[0].reason_code) == (None, "R")
    assert (results[1].decision, results[1].reason_code) == (None, None)
    assert (results[2].decision, results[2].reason_code) == ("WAIT", "NO_SIGNAL")
    assert not hasattr(results[2], "data")


def test_recent_decision_reasons_rejects_limit_above_repository_bound(database: tuple[Engine, Session]) -> None:
    _, session = database

    with pytest.raises(ValueError, match="limit"):
        TradingAuditRepository(session).list_recent_decision_reasons("scope-1", "automation-1", 21)
