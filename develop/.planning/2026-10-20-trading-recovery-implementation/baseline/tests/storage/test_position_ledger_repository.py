"""Baseline position ledger repository tests."""

from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    ExecutionLotAllocationDraft,
    PositionCycleDraft,
    PositionCycleState,
    PositionLotDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models import ExecutionLotAllocationModel, PositionCycleModel
from moex_sentinel.storage.repositories.position_ledger import PositionLedgerRepository
from tests.domain.trading_facts_helpers import all_fact_drafts
from tests.storage.trading_facts_helpers import instrument_model, seed_buy_and_sell_executions


@pytest.fixture
def database(core_database: tuple[Engine, Session]) -> tuple[Engine, Session]:
    """Seed the BUY/SELL history used by this repository's scenarios."""
    _, session = core_database
    seed_buy_and_sell_executions(session)
    return core_database


def fact_value(value_type):
    return next(value for value in all_fact_drafts() if isinstance(value, value_type))


def test_allocation_retry_does_not_duplicate_realized_attribution(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    lot = fact_value(PositionLotDraft).model_copy(update={"buy_execution_id": "execution-buy"})
    allocation = fact_value(ExecutionLotAllocationDraft).model_copy(update={"sell_execution_id": "execution-sell"})
    repository.append_lot("scope-1", lot)

    first = repository.append_allocation("scope-1", allocation)
    second = repository.append_allocation("scope-1", allocation.model_copy(deep=True))

    assert second == first
    assert session.scalar(select(func.count()).select_from(ExecutionLotAllocationModel)) == 1


def test_open_lots_are_returned_in_lifo_order(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    first = fact_value(PositionLotDraft).model_copy(update={"id": "lot-1", "buy_execution_id": "execution-buy"})
    second = first.model_copy(update={"id": "lot-2", "buy_execution_id": "execution-sell"})
    repository.append_lot("scope-1", first)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_lot("scope-1", second)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert repository.list_open_lots("scope-1", "cycle-1") == (first,)


def test_allocation_requires_sell_execution(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    lot = fact_value(PositionLotDraft).model_copy(update={"buy_execution_id": "execution-buy"})
    repository.append_lot("scope-1", lot)
    allocation = fact_value(ExecutionLotAllocationDraft).model_copy(update={"sell_execution_id": "execution-buy"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_allocation("scope-1", allocation)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE


def test_cycle_aggregate_can_be_replaced_without_editing_history(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    current = repository.get_cycle("scope-1", "cycle-1")
    changed = current.model_copy(update={"realized_pnl": current.realized_pnl + 1, "net_pnl": current.net_pnl + 1})

    result = repository.replace_cycle_aggregate("scope-1", changed)

    assert result.realized_pnl == current.realized_pnl + 1
    assert result.net_pnl == current.net_pnl + 1


def test_position_repository_rejects_cross_scope_value(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    cycle = fact_value(PositionCycleDraft).model_copy(update={"user_broker_id": "scope-2"})

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.open_cycle("scope-1", cycle)

    assert caught.value.code is TradingFactErrorCode.CROSS_SCOPE


def test_cycle_requires_automation_instrument_and_accepts_matching_replay(database: tuple[Engine, Session]) -> None:
    _, session = database
    session.add(instrument_model("instrument-3", "scope-1"))
    session.flush()
    repository = PositionLedgerRepository(session)
    original = fact_value(PositionCycleDraft)
    mismatched = original.model_copy(
        update={
            "id": "cycle-mismatched",
            "instrument_id": "instrument-3",
            "state": PositionCycleState.CLOSED,
            "closed_at": original.opened_at,
        }
    )

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.open_cycle("scope-1", mismatched)

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 1
    matching = mismatched.model_copy(update={"instrument_id": "instrument-1"})
    assert repository.open_cycle("scope-1", matching) == matching
    assert repository.open_cycle("scope-1", matching.model_copy(deep=True)) == matching
    assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 2


def test_allocation_and_lot_decrement_are_accepted_atomically(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    lot = fact_value(PositionLotDraft).model_copy(update={"buy_execution_id": "execution-buy"})
    allocation = fact_value(ExecutionLotAllocationDraft).model_copy(update={"sell_execution_id": "execution-sell"})
    repository.append_lot("scope-1", lot)

    saved = repository.append_allocation_and_decrement(
        "scope-1",
        allocation,
        expected_remaining_lots=1,
        remaining_lots_after=0,
    )

    assert saved == allocation
    assert repository.list_open_lots("scope-1", "cycle-1") == ()
    assert repository.list_allocations("scope-1", "cycle-1") == (allocation,)


def test_stale_lot_balance_rejects_allocation_without_mutation(database: tuple[Engine, Session]) -> None:
    _, session = database
    repository = PositionLedgerRepository(session)
    lot = fact_value(PositionLotDraft).model_copy(update={"buy_execution_id": "execution-buy"})
    allocation = fact_value(ExecutionLotAllocationDraft).model_copy(update={"sell_execution_id": "execution-sell"})
    repository.append_lot("scope-1", lot)

    with pytest.raises(TradingFactPersistenceError) as caught:
        repository.append_allocation_and_decrement(
            "scope-1",
            allocation,
            expected_remaining_lots=2,
            remaining_lots_after=1,
        )

    assert caught.value.code is TradingFactErrorCode.INVALID_STATE
    assert repository.list_open_lots("scope-1", "cycle-1") == (lot,)
    assert repository.list_allocations("scope-1", "cycle-1") == ()


def test_older_cycle_valuation_cannot_overwrite_newer_aggregate(database):
    _, session = database
    repository = PositionLedgerRepository(session)
    current = repository.get_cycle("scope-1", "cycle-1")
    newer = current.model_copy(
        update={"net_pnl": current.net_pnl + 10, "updated_at": current.updated_at + timedelta(seconds=1)}
    )
    repository.replace_cycle_aggregate("scope-1", newer)
    result = repository.replace_cycle_aggregate("scope-1", current)
    assert result.net_pnl == newer.net_pnl
    assert result.updated_at == newer.updated_at
