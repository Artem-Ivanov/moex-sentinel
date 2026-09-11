"""Atomic baseline persistence for adopted broker positions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.domain.position_adoption import (
    PositionAdoptionCandidate,
    PositionAdoptionConflictError,
    PositionAdoptionWriteResult,
)
from moex_sentinel.storage.models import Base, PositionCycleModel, PositionLotModel, TradingAutomationModel
from moex_sentinel.storage.repositories.position_adoption import PositionAdoptionRepository
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

SCOPE_ID = "00000000-0000-4000-8000-000000000101"
INSTRUMENT_ID = "00000000-0000-4000-8000-000000000102"
NOW = datetime(2026, 8, 14, 10, 0, 0, 123000, tzinfo=UTC)
LATER = datetime(2026, 8, 14, 10, 5, 0, 456000, tzinfo=UTC)


def candidate(
    *,
    average_price: Decimal = Decimal("100"),
    observed_at: datetime = NOW,
) -> PositionAdoptionCandidate:
    return PositionAdoptionCandidate.create(
        user_broker_id=SCOPE_ID,
        instrument_id=INSTRUMENT_ID,
        quantity_lots=2,
        average_price=average_price,
        invested_amount=average_price * 20,
        currency="RUB",
        observed_at=observed_at,
    )


def repository() -> tuple[PositionAdoptionRepository, sessionmaker]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(user_broker_model(SCOPE_ID, "account-1"))
        session.add(instrument_model(INSTRUMENT_ID, SCOPE_ID))
    return PositionAdoptionRepository(factory), factory


def test_adoption_writes_one_hold_snapshot_without_synthetic_history() -> None:
    repo, factory = repository()
    value = candidate()

    outcome = repo.adopt(value)

    assert outcome is PositionAdoptionWriteResult.ADOPTED
    with factory() as session:
        automation = session.scalar(select(TradingAutomationModel))
        assert automation is not None
        assert automation.state == "HOLD"
        assert automation.hold_reason == "BOOTSTRAPPING"
        assert automation.bootstrap_position_cycle_id == str(value.position_cycle_id)
        assert automation.bootstrap_position_lot_id == str(value.position_lot_id)
        assert automation.bootstrap_quantity_lots == 2
        assert automation.bootstrap_average_price == Decimal("100")
        assert automation.bootstrap_invested_amount == Decimal("2000")
        assert automation.bootstrap_currency == "RUB"
        assert session.scalar(select(PositionCycleModel)) is None
        assert session.scalar(select(PositionLotModel)) is None


def test_exact_retry_is_idempotent_and_conflicting_retry_fails_safely() -> None:
    repo, factory = repository()
    assert repo.adopt(candidate()) is PositionAdoptionWriteResult.ADOPTED
    assert repo.adopt(candidate()) is PositionAdoptionWriteResult.EXISTING

    with pytest.raises(PositionAdoptionConflictError):
        repo.adopt(candidate(average_price=Decimal("101")))

    with factory() as session:
        assert len(session.scalars(select(TradingAutomationModel)).all()) == 1


def test_same_position_observed_later_is_an_idempotent_retry() -> None:
    repo, factory = repository()
    assert repo.adopt(candidate()) is PositionAdoptionWriteResult.ADOPTED

    assert repo.adopt(candidate(observed_at=LATER)) is PositionAdoptionWriteResult.EXISTING

    with factory() as session:
        automation = session.scalar(select(TradingAutomationModel))
        assert automation is not None
        assert automation.bootstrap_observed_at == NOW


def test_existing_managed_automation_is_not_replaced() -> None:
    repo, factory = repository()
    value = candidate()
    with factory.begin() as session:
        session.add(
            TradingAutomationModel(
                id=str(value.automation_id),
                user_broker_id=SCOPE_ID,
                instrument_id=INSTRUMENT_ID,
                state="IN_WORK",
                suspended_from_state=None,
                hold_reason=None,
                revision=1,
                last_sequence_number=0,
                resume_requested=False,
                closed_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    assert repo.adopt(value) is PositionAdoptionWriteResult.EXISTING


def test_external_instrument_lookup_is_scoped() -> None:
    repo, _factory = repository()

    assert repo.find_instrument(SCOPE_ID, f"external-{INSTRUMENT_ID}") is not None
    assert repo.find_instrument("00000000-0000-4000-8000-000000000999", f"external-{INSTRUMENT_ID}") is None


@pytest.mark.parametrize("new_price", [Decimal("100"), Decimal("101")])
def test_closed_positions_allow_distinct_generations_with_idempotent_later_retry(new_price: Decimal) -> None:
    repo, factory = repository()
    assert repo.adopt(candidate()) is PositionAdoptionWriteResult.ADOPTED
    previous_ids: set[str] = set()
    previous_cycle_ids: set[str] = set()
    previous_lot_ids: set[str] = set()
    closed_snapshots: dict[str, tuple] = {}

    for _generation in range(3):
        with factory.begin() as session:
            active = session.scalar(select(TradingAutomationModel).where(TradingAutomationModel.closed_at.is_(None)))
            assert active is not None
            assert active.id not in previous_ids
            assert active.bootstrap_position_cycle_id not in previous_cycle_ids
            assert active.bootstrap_position_lot_id not in previous_lot_ids
            previous_ids.add(active.id)
            previous_cycle_ids.add(active.bootstrap_position_cycle_id)
            previous_lot_ids.add(active.bootstrap_position_lot_id)
            active.state = "CLOSED"
            active.closed_at = LATER
            closed_snapshots[active.id] = (
                active.bootstrap_position_cycle_id,
                active.bootstrap_position_lot_id,
                active.bootstrap_average_price,
                active.bootstrap_observed_at,
            )

        assert repo.adopt(candidate(average_price=new_price, observed_at=LATER)) is PositionAdoptionWriteResult.ADOPTED
        assert (
            repo.adopt(candidate(average_price=new_price, observed_at=LATER + timedelta(seconds=1)))
            is PositionAdoptionWriteResult.EXISTING
        )
        with pytest.raises(PositionAdoptionConflictError):
            repo.adopt(candidate(average_price=new_price + 1, observed_at=LATER))

    with factory() as session:
        rows = session.scalars(select(TradingAutomationModel)).all()
        assert len(rows) == 4
        assert str(candidate().automation_id) in {row.id for row in rows}
        for row in rows:
            if row.id in closed_snapshots:
                assert row.state == "CLOSED"
                assert row.closed_at == LATER
                assert (
                    row.bootstrap_position_cycle_id,
                    row.bootstrap_position_lot_id,
                    row.bootstrap_average_price,
                    row.bootstrap_observed_at,
                ) == closed_snapshots[row.id]


def test_successor_unique_conflict_rereads_the_existing_snapshot() -> None:
    repo, factory = repository()
    repo.adopt(candidate())
    with factory.begin() as session:
        first = session.get_one(TradingAutomationModel, str(candidate().automation_id))
        first.state = "CLOSED"
        first.closed_at = LATER
    assert repo.adopt(candidate(observed_at=LATER)) is PositionAdoptionWriteResult.ADOPTED

    class StaleActiveReadRepository(PositionAdoptionRepository):
        first_read = True

        def _active(self, session, value):
            if self.first_read:
                self.first_read = False
                return None
            return super()._active(session, value)

    contender = StaleActiveReadRepository(factory)

    assert contender.adopt(candidate(observed_at=NOW)) is PositionAdoptionWriteResult.EXISTING
    with factory() as session:
        assert len(session.scalars(select(TradingAutomationModel)).all()) == 2
