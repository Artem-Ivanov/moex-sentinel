"""Baseline trading analytics repository tests."""

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    BrokerAccountFeeProfileDraft,
    PositionValuationSnapshotDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models import PositionValuationSnapshotModel
from moex_sentinel.storage.repositories.trading_analytics import TradingAnalyticsRepository
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import automation_model, instrument_model, position_cycle_model, seed_cycle


@pytest.fixture
def database(core_database: tuple[Engine, Session]) -> tuple[Engine, Session]:
    """Seed the cycle required by these analytics scenarios."""
    _, session = core_database
    seed_cycle(session)
    return core_database


def fact_value(value_type):
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


def test_fee_profile_upsert_preserves_identity_and_scope_key(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAnalyticsRepository(session)
    original = fact_value(BrokerAccountFeeProfileDraft)
    changed = original.model_copy(update={"id": "ignored-id", "buy_rate": original.buy_rate + 1})

    first = repository.upsert_fee_profile("scope-1", original)
    second = repository.upsert_fee_profile("scope-1", changed)

    assert first.id == original.id
    assert second.id == original.id
    assert second.buy_rate == original.buy_rate + 1
    assert second.user_broker_id == "scope-1"


def test_position_valuations_are_immutable_appends(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAnalyticsRepository(session)
    valuation = fact_value(PositionValuationSnapshotDraft)

    assert repository.append_position_valuation("scope-1", valuation) == valuation
    repository.append_position_valuation("scope-1", valuation.model_copy(update={"id": "valuation-2"}))

    assert session.scalar(select(func.count()).select_from(PositionValuationSnapshotModel)) == 2


def test_analytics_repository_rejects_cross_scope_value(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = TradingAnalyticsRepository(session)
    valuation = fact_value(PositionValuationSnapshotDraft).model_copy(update={"user_broker_id": "scope-2"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_position_valuation("scope-1", valuation)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE


@pytest.mark.parametrize("changed_field", ["automation_id", "instrument_id"])
def test_valuation_rejects_same_scope_mismatched_lineage_without_mutation(
    database: tuple[Engine, Session],
    changed_field: str,
) -> None:
    _, session = database
    session.add(instrument_model("instrument-3", "scope-1"))
    session.add(automation_model("automation-2", state="CLOSED", closed=True))
    session.flush()
    original = fact_value(PositionValuationSnapshotDraft)
    mismatched = original.model_copy(
        update={changed_field: {"automation_id": "automation-2", "instrument_id": "instrument-3"}[changed_field]}
    )
    repository = TradingAnalyticsRepository(session)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_position_valuation("scope-1", mismatched)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
    assert session.scalar(select(func.count()).select_from(PositionValuationSnapshotModel)) == 0
    assert repository.append_position_valuation("scope-1", original) == original
    assert repository.append_position_valuation("scope-1", original) == original
    assert session.scalar(select(func.count()).select_from(PositionValuationSnapshotModel)) == 1


def test_valuation_rejects_cycle_whose_instrument_differs_from_automation(
    database: tuple[Engine, Session],
) -> None:
    _, session = database
    session.add(instrument_model("instrument-3", "scope-1"))
    session.flush()
    # Previously accepted malformed lineage must not spread into new valuations.
    cycle = position_cycle_model("cycle-2", state="CLOSED", closed=True)
    cycle.instrument_id = "instrument-3"
    session.add(cycle)
    session.flush()
    valuation = fact_value(PositionValuationSnapshotDraft).model_copy(
        update={"position_cycle_id": "cycle-2", "instrument_id": "instrument-3"}
    )

    with pytest.raises(TradingFactPersistenceError) as caught:
        TradingAnalyticsRepository(session).append_position_valuation("scope-1", valuation)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE
    assert session.scalar(select(func.count()).select_from(PositionValuationSnapshotModel)) == 0
