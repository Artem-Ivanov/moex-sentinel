"""Characterize both ledger entrypoints before consolidating their Session helpers."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.trading_facts import ExecutionLotAllocatedPayload
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import (
    INTENT_ID,
    NOW,
    SELL_INTENT_ID,
    baseline_command,
    filled_buy,
)
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import (
    Base,
    CachedAutomationModel,
    FactOutboxModel,
    LocalIntentModel,
    LotAllocationModel,
    TradeLotModel,
    TradingCycleStateModel,
)
from trading_automaton.storage.repository import IntentBatchItem, LocalAutomationRepository, TradingCycleState

AUTOMATION_ID = str(baseline_command().automation_id)
OLDER_LOT_ID = "00000000-0000-4000-8000-000000000001"
NEWER_LOT_ID = "00000000-0000-4000-8000-000000000002"
CYCLE_ID = "00000000-0000-4000-8000-000000000400"
CLOSED_AT = NOW + timedelta(minutes=2, milliseconds=123)


@pytest.fixture
def storage(tmp_path):
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'ledger.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    repository.cache_command(baseline_command())
    try:
        yield repository, factory, engine
    finally:
        engine.dispose()


def execution(repository, *, side, lots, terminal="FILLED", price="100", commission="2", executed_at=CLOSED_AT):
    intent_id = INTENT_ID if side == "BUY" else SELL_INTENT_ID
    kind = "BUY_MORE" if side == "BUY" else "SELL_PART"
    ordered_lots = lots if terminal == "FILLED" else lots + 1
    item = decision_item(baseline_command(), intent_id=intent_id).model_copy(
        update={
            "decision": kind,
            "decision_quantity_lots": ordered_lots,
            "limit_price": Decimal(price),
            "intent": IntentBatchItem(intent_id, kind, side, ordered_lots, Decimal(price)),
        }
    )
    repository.save_decision_batch((item,), occurred_at=NOW)
    return filled_buy().model_copy(
        update={
            "intent_id": intent_id,
            "side": side,
            "state": terminal,
            "quantity_lots": ordered_lots,
            "executed_lots": lots,
            "requested_price": Decimal(price),
            "executed_price": Decimal(price),
            "requested_amount": Decimal(price) * 10 * ordered_lots,
            "executed_amount": Decimal(price) * 10 * lots,
            "executed_commission": Decimal(commission),
            "occurred_at": CLOSED_AT,
            "executed_at": executed_at,
            "terminal_at": CLOSED_AT,
        }
    )


def seed_lots(factory, *, equal_times=False, fractional=False):
    with factory.begin() as session:
        session.get_one(CachedAutomationModel, AUTOMATION_ID).position_cycle_id = CYCLE_ID
        session.add_all(
            [
                TradeLotModel(
                    id=OLDER_LOT_ID,
                    automation_id=AUTOMATION_ID,
                    source_intent_id="00000000-0000-4000-8000-000000000401",
                    source="EXECUTED",
                    original_lots=3 if fractional else 2,
                    remaining_lots=3 if fractional else 2,
                    entry_price=Decimal("100"),
                    entry_commission=Decimal("1" if fractional else "2"),
                    opened_at=NOW,
                ),
                TradeLotModel(
                    id=NEWER_LOT_ID,
                    automation_id=AUTOMATION_ID,
                    source_intent_id="00000000-0000-4000-8000-000000000402",
                    source="EXECUTED",
                    original_lots=2,
                    remaining_lots=2,
                    entry_price=Decimal("90"),
                    entry_commission=Decimal("2"),
                    opened_at=NOW if equal_times else NOW + timedelta(minutes=1),
                ),
            ]
        )


def apply_ledger(repository, finalization):
    if finalization.side == "BUY":
        return repository.create_trade_lot(
            automation_id=AUTOMATION_ID,
            source_intent_id=finalization.intent_id,
            source="EXECUTED",
            quantity_lots=finalization.executed_lots,
            entry_price=finalization.executed_price,
            entry_commission=finalization.executed_commission,
            opened_at=finalization.executed_at or finalization.occurred_at,
        )
    return repository.allocate_sell_lifo(
        automation_id=AUTOMATION_ID,
        sell_intent_id=finalization.intent_id,
        quantity_lots=finalization.executed_lots,
        exit_price=finalization.executed_price,
        exit_commission=finalization.executed_commission,
        closed_at=finalization.executed_at or finalization.occurred_at,
        lot_size=finalization.lot_size,
    )


def allocations(factory):
    with factory() as session:
        return session.execute(
            select(
                LotAllocationModel.lot_id,
                LotAllocationModel.quantity_lots,
                LotAllocationModel.exit_price,
                LotAllocationModel.exit_commission,
                LotAllocationModel.realized_pnl,
                LotAllocationModel.closed_at,
            )
            .join(TradeLotModel, LotAllocationModel.lot_id == TradeLotModel.id)
            .order_by(TradeLotModel.opened_at.desc(), TradeLotModel.id.desc())
        ).all()


def persisted_state(factory):
    models = (
        CachedAutomationModel,
        LocalIntentModel,
        TradeLotModel,
        LotAllocationModel,
        TradingCycleStateModel,
        FactOutboxModel,
    )
    with factory() as session:
        return {
            model.__tablename__: sorted((tuple(row) for row in session.execute(select(model.__table__))), key=repr)
            for model in models
        }


@pytest.mark.parametrize("entrypoint", ["ledger", "atomic"])
@pytest.mark.parametrize("terminal", ["FILLED", "CANCELLED", "REJECTED"])
@pytest.mark.parametrize("executed_at", [CLOSED_AT, None])
def test_buy_entrypoints_preserve_lot_values_and_execution_time(storage, entrypoint, terminal, executed_at):
    repository, factory, _ = storage
    finalization = execution(repository, side="BUY", lots=2, terminal=terminal, executed_at=executed_at)
    if entrypoint == "atomic":
        result = repository.finalize_execution(finalization)
        assert result.applied
    else:
        record = apply_ledger(repository, finalization)
        assert record.source_intent_kind == "BUY_MORE"
        assert record.remaining_lots == 2
    with factory() as session:
        lot = session.scalars(select(TradeLotModel)).one()
        assert (
            lot.automation_id,
            lot.source_intent_id,
            lot.source,
            lot.original_lots,
            lot.remaining_lots,
            lot.entry_price,
            lot.entry_commission,
            lot.opened_at,
        ) == (AUTOMATION_ID, INTENT_ID, "EXECUTED", 2, 2, Decimal("100"), Decimal("2"), CLOSED_AT)


@pytest.mark.parametrize("entrypoint", ["ledger", "atomic"])
@pytest.mark.parametrize("terminal", ["FILLED", "CANCELLED", "REJECTED"])
@pytest.mark.parametrize("equal_times", [False, True])
def test_sell_entrypoints_follow_lifo_with_both_fees_and_lot_size(storage, entrypoint, terminal, equal_times):
    repository, factory, _ = storage
    seed_lots(factory, equal_times=equal_times)
    finalization = execution(repository, side="SELL", lots=3, terminal=terminal, price="110", commission="3")
    if entrypoint == "atomic":
        result = repository.finalize_execution(finalization)
        assert result.applied
        assert Decimal(result.authoritative_position_snapshot["realized_pnl"]) == Decimal("494")
        assert Decimal(result.authoritative_position_snapshot["actual_commissions"]) == Decimal("7")
    else:
        assert apply_ledger(repository, finalization) is None
    assert allocations(factory) == [
        (NEWER_LOT_ID, 2, Decimal("110"), Decimal("2"), Decimal("396"), CLOSED_AT),
        (OLDER_LOT_ID, 1, Decimal("110"), Decimal("1"), Decimal("98"), CLOSED_AT),
    ]
    assert repository.realized_pnl(AUTOMATION_ID) == Decimal("494")
    assert [(lot.id, lot.remaining_lots) for lot in repository.list_open_lots(AUTOMATION_ID)] == [(OLDER_LOT_ID, 1)]


@pytest.mark.parametrize("entrypoint", ["ledger", "atomic"])
def test_fractional_commissions_preserve_persisted_decimal_results(storage, entrypoint):
    repository, factory, _ = storage
    seed_lots(factory, fractional=True)
    finalization = execution(repository, side="SELL", lots=4, price="110", commission="1")
    if entrypoint == "atomic":
        repository.finalize_execution(finalization)
    else:
        apply_ledger(repository, finalization)
    # Newest: (110-90)*10*2 - 2 - .5 = 397.5.
    # Older: (110-100)*10*2 - 2/3 - .5 = 198.833333333 at persisted scale9.
    assert allocations(factory) == [
        (NEWER_LOT_ID, 2, Decimal("110"), Decimal("0.5"), Decimal("397.5"), CLOSED_AT),
        (OLDER_LOT_ID, 2, Decimal("110"), Decimal("0.5"), Decimal("198.833333333"), CLOSED_AT),
    ]
    assert repository.realized_pnl(AUTOMATION_ID) == Decimal("596.333333333")


@pytest.mark.parametrize("entrypoint", ["ledger", "atomic"])
def test_insufficient_lots_leave_all_durable_execution_state_unchanged(storage, entrypoint):
    repository, factory, _ = storage
    seed_lots(factory)
    finalization = execution(repository, side="SELL", lots=5, price="110")
    before = persisted_state(factory)
    finalize = (
        repository.finalize_execution if entrypoint == "atomic" else lambda value: apply_ledger(repository, value)
    )
    with pytest.raises(ValueError, match="Sell execution exceeds the worker lot ledger"):
        finalize(finalization)
    assert persisted_state(factory) == before


def test_reconciled_lots_without_intent_remain_distinct_and_return_their_source(storage):
    repository, factory, _ = storage
    values = {
        "automation_id": AUTOMATION_ID,
        "source_intent_id": None,
        "source": "RECONCILED",
        "quantity_lots": 2,
        "entry_price": Decimal("94.14"),
        "entry_commission": Decimal("0"),
        "opened_at": NOW,
    }
    first = repository.create_trade_lot(**values)
    second = repository.create_trade_lot(**values)
    assert first.id != second.id
    assert first.source_intent_id is None
    assert first.source_intent_kind is None
    assert first.source == "RECONCILED"
    assert first.entry_price == Decimal("94.14")
    with factory() as session:
        assert len(session.scalars(select(TradeLotModel)).all()) == 2


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_public_duplicate_execution_keeps_existing_ledger_without_revalidating_new_values(storage, side):
    repository, factory, _ = storage
    if side == "SELL":
        seed_lots(factory)
    finalization = execution(repository, side=side, lots=1)
    first = apply_ledger(repository, finalization)
    before = persisted_state(factory)
    duplicate = apply_ledger(
        repository,
        finalization.model_copy(
            update={"executed_lots": 99, "executed_price": Decimal("999"), "executed_commission": Decimal("99")}
        ),
    )
    assert persisted_state(factory) == before
    if side == "BUY":
        assert duplicate == first
        assert duplicate.source_intent_kind == "BUY_MORE"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_atomic_replay_after_reopening_keeps_graph_and_rejects_conflicting_terminal_result(storage, side):
    repository, factory, engine = storage
    if side == "SELL":
        seed_lots(factory)
    finalization = execution(repository, side=side, lots=1)
    assert repository.finalize_execution(finalization).applied
    before = persisted_state(factory)
    engine.dispose()
    reopened = LocalAutomationRepository(factory)
    assert reopened.finalize_execution(finalization).applied is False
    with pytest.raises(ValueError, match="Execution replay conflicts"):
        reopened.finalize_execution(finalization.model_copy(update={"executed_commission": Decimal("99")}))
    assert persisted_state(factory) == before


class FailAfterAllocation(LocalAutomationRepository):
    def _finalize_trading_cycle(self, session, intent, finalization):
        session.flush()
        raise RuntimeError("Synthetic failure after allocation")  # noqa: TRY004 - injected failure


class FailAfterAllocationFact(FactOutboxWriter):
    def append(self, session, cached, **values):
        result = super().append(session, cached, **values)
        if isinstance(values["payload"], ExecutionLotAllocatedPayload):
            raise RuntimeError("Synthetic failure after allocation fact")  # noqa: TRY004 - injected failure
        return result


@pytest.mark.parametrize("failure", ["after_allocation", "outbox"])
def test_sell_failure_rolls_back_intent_lots_cycle_sequence_revision_and_outbox(storage, failure):
    repository, factory, _ = storage
    seed_lots(factory)
    repository.save_cycle_state(TradingCycleState(AUTOMATION_ID, Decimal("99"), NOW, True, None, NOW))
    finalization = execution(repository, side="SELL", lots=3, price="110", commission="3")
    before = persisted_state(factory)
    if failure == "after_allocation":
        failing = FailAfterAllocation(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    else:
        failing = LocalAutomationRepository(factory, fact_writer=FailAfterAllocationFact(clock=lambda: NOW))
    with pytest.raises(RuntimeError, match="Synthetic failure after allocation"):
        failing.finalize_execution(finalization)
    assert persisted_state(factory) == before
    assert repository.finalize_execution(finalization).applied
    assert repository.realized_pnl(AUTOMATION_ID) == Decimal("494")
