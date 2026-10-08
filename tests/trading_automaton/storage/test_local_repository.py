"""Baseline durability rules for the Worker-private SQLite database."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread, current_thread
from uuid import UUID

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from sentinel_contracts.automation_lifecycle import InvalidAutomationTransition
from sentinel_contracts.broker_execution import BrokerOrderState, OrderSide
from sentinel_contracts.business_audit import BusinessAuditEvent, BusinessAuditLevel, BusinessAuditStage
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import BrokerPositionBootstrap, FactKind
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import (
    AUDIT_ID,
    AUDIT_PROCESS_ID,
    INTENT_ID,
    NOW,
    SELL_INTENT_ID,
    baseline_command,
    filled_buy,
)
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.services.batch_runtime import BatchTradingRuntimeService
from trading_automaton.services.order_dispatch import OrderDispatchService
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import (
    Base,
    BusinessAuditEventModel,
    CachedAutomationModel,
    FactOutboxModel,
    LocalIntentModel,
    TradeDecisionModel,
    TradeLotModel,
)
from trading_automaton.storage.repository import ExecutionFinalization, IntentBatchItem, LocalAutomationRepository


def test_cache_round_trip_uses_strategy_free_baseline_command(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()

    assert repo.cache_command(command_value) is True
    restored = repo.list_active()

    assert restored == [command_value]
    assert not hasattr(restored[0], "strategy")
    assert restored[0].currency == "RUB"


def test_decision_batch_emits_typed_decision_and_order_facts(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()
    repo.cache_command(command_value)

    result = repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    facts = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)

    assert len(result.decisions) == 1
    assert result.intents[0].idempotency_key == INTENT_ID
    assert [fact.fact_kind for fact in facts] == [
        FactKind.TRADE_DECISION_RECORDED.value,
        FactKind.BROKER_ORDER_RECORDED.value,
    ]
    assert [fact.sequence_number for fact in facts] == [1, 2]
    assert facts[1].payload["decision_id"] == facts[0].payload["decision_id"]


def test_order_state_fact_preserves_recorded_order_intent_snapshot(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    repo.cache_command(command_value)
    position_cycle_id = "00000000-0000-4000-8000-000000000409"
    with factory.begin() as session:
        cached = session.get_one(CachedAutomationModel, str(command_value.automation_id))
        cached.position_cycle_id = position_cycle_id

    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    repo.update_intent(
        INTENT_ID,
        state="SUBMITTING",
        occurred_at=NOW + timedelta(milliseconds=2),
        dispatch_started_at=NOW + timedelta(milliseconds=2),
    )
    facts = repo.ready_fact_outbox(10, now=NOW + timedelta(seconds=1), deadline_ms=0)
    recorded = facts[1].payload
    changed = facts[2].payload

    decimal_fields = {"limit_price", "requested_amount", "estimated_commission"}
    for field in (
        "order_id",
        "decision_id",
        "position_cycle_id",
        "instrument_id",
        "idempotency_key",
        "intent_kind",
        "side",
        "order_type",
        "quantity_lots",
        "limit_price",
        "requested_amount",
        "estimated_commission",
        "strategy_snapshot",
        "created_at",
    ):
        if field in decimal_fields:
            assert Decimal(str(changed[field])) == Decimal(str(recorded[field]))
        else:
            assert changed[field] == recorded[field]


def test_active_buy_reservation_uses_cached_command_currency(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()
    repo.cache_command(command_value)
    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)

    reservation = repo.list_active_buy_intent_reservations()[0]

    assert reservation.currency == "RUB"
    assert reservation.amount == Decimal("1001")


def test_terminal_buy_is_idempotent_and_emits_restart_safe_execution_graph(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)

    first = repo.finalize_execution(filled_buy())
    replay = repo.finalize_execution(filled_buy())
    facts = repo.ready_fact_outbox(20, now=NOW + timedelta(seconds=1), deadline_ms=0)

    assert first.applied is True
    assert replay.applied is False
    assert [fact.fact_kind for fact in facts[-4:]] == [
        FactKind.BROKER_ORDER_STATE_CHANGED.value,
        FactKind.POSITION_CYCLE_UPDATED.value,
        FactKind.TRADE_EXECUTION_RECORDED.value,
        FactKind.POSITION_LOT_OPENED.value,
    ]
    assert len(repo.list_open_lots(automation_id)) == 1


@pytest.mark.parametrize("acknowledge_terminal_facts", [False, True])
def test_decision_fact_sequence_uses_terminal_facts_committed_after_stale_cache_read(
    tmp_path: Path,
    acknowledge_terminal_facts: bool,
) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    wait_item = decision_item(
        command_value,
        process_id="00000000-0000-4000-8000-000000000414",
    ).model_copy(
        update={
            "estimated_commission": Decimal(),
            "decision": "WAIT",
            "reason_code": "ACTIVE_INTENT",
            "decision_quantity_lots": 0,
            "limit_price": None,
            "intent": None,
        }
    )
    stale_loaded = Event()
    release_stale = Event()
    errors: list[BaseException] = []

    def pause_stale_decision(session: Session, _flush_context: object, _instances: object) -> None:
        if current_thread().name != "stale-decision":
            return
        if not any(isinstance(model, TradeDecisionModel) for model in session.new):
            return
        stale_loaded.set()
        if not release_stale.wait(timeout=2):
            raise TimeoutError("Synthetic stale decision was not released")

    def save_wait_decision() -> None:
        try:
            repo.save_decision_batch((wait_item,), occurred_at=NOW + timedelta(milliseconds=20))
        except BaseException as error:
            errors.append(error)

    event.listen(Session, "before_flush", pause_stale_decision)
    thread = Thread(target=save_wait_decision, name="stale-decision")
    try:
        thread.start()
        assert stale_loaded.wait(timeout=1)
        repo.finalize_execution(filled_buy())
        with factory() as session:
            committed = session.get_one(CachedAutomationModel, automation_id)
            committed_sequence = committed.last_sequence_number
            committed_revision = committed.revision
        if acknowledge_terminal_facts:
            repo.acknowledge_fact_outbox(
                automation_id,
                accepted_through_sequence=committed_sequence,
                current_revision=committed_revision,
            )
        release_stale.set()
        thread.join(timeout=2)
    finally:
        release_stale.set()
        thread.join(timeout=2)
        event.remove(Session, "before_flush", pause_stale_decision)

    assert errors == []
    with factory() as session:
        cached_sequence = session.get_one(CachedAutomationModel, automation_id).last_sequence_number
        sequences = session.scalars(
            select(FactOutboxModel.sequence_number).order_by(FactOutboxModel.sequence_number)
        ).all()
    assert cached_sequence == committed_sequence + 1
    if acknowledge_terminal_facts:
        assert sequences == [committed_sequence + 1]
    else:
        assert sequences == list(range(1, len(sequences) + 1))


def test_reconciled_terminal_sell_emits_facts_without_double_applying_ledger(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    repo.finalize_execution(filled_buy())
    sell_item = decision_item(
        command_value,
        intent_id=SELL_INTENT_ID,
        process_id="00000000-0000-4000-8000-000000000411",
    ).model_copy(
        update={
            "decision": "SELL_PART",
            "reason_code": "TEST_SELL",
            "decision_quantity_lots": 1,
            "limit_price": Decimal("110"),
            "intent": IntentBatchItem(SELL_INTENT_ID, "SELL_PART", "SELL", 1, Decimal("110")),
        }
    )
    repo.save_decision_batch((sell_item,), occurred_at=NOW + timedelta(milliseconds=20))
    repo.allocate_sell_lifo(
        automation_id=automation_id,
        sell_intent_id=SELL_INTENT_ID,
        quantity_lots=1,
        exit_price=Decimal("110"),
        exit_commission=Decimal("1"),
        closed_at=NOW + timedelta(milliseconds=30),
        lot_size=10,
    )

    result = repo.finalize_execution(
        ExecutionFinalization(
            intent_id=SELL_INTENT_ID,
            automation_id=automation_id,
            broker_id=str(command_value.broker_id),
            account_id=command_value.account_id,
            instrument_id=command_value.external_instrument_id,
            side="SELL",
            quantity_lots=1,
            requested_price=Decimal("110"),
            currency="RUB",
            state="FILLED",
            occurred_at=NOW + timedelta(milliseconds=31),
            broker_order_id="synthetic-sell-order",
            requested_amount=Decimal("1100"),
            executed_amount=Decimal("1100"),
            estimated_commission=Decimal("1"),
            executed_commission=Decimal("1"),
            executed_lots=1,
            executed_price=Decimal("110"),
            executed_at=NOW + timedelta(milliseconds=30),
            terminal_at=NOW + timedelta(milliseconds=31),
            lot_size=10,
            process_id="00000000-0000-4000-8000-000000000411",
        ),
        ledger_already_applied=True,
    )

    assert result.applied is True
    assert repo.list_open_lots(automation_id) == []
    with factory() as session:
        intent = session.get_one(LocalIntentModel, SELL_INTENT_ID)
        assert intent.execution_facts_emitted_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(FactOutboxModel)
                .where(
                    FactOutboxModel.fact_kind == FactKind.TRADE_EXECUTION_RECORDED.value,
                    FactOutboxModel.payload["broker_order_id"].as_string() == SELL_INTENT_ID,
                )
            )
            == 1
        )


def test_audit_is_journaled_once_and_delivered_only_as_typed_fact(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    event = BusinessAuditEvent(
        event_id=AUDIT_ID,
        process_id=AUDIT_PROCESS_ID,
        parent_process_id=None,
        automation_id=automation_id,
        broker_id=str(command_value.broker_id),
        account_id=command_value.account_id,
        instrument_id=command_value.external_instrument_id,
        level=BusinessAuditLevel.INFO,
        stage=BusinessAuditStage.STRATEGY_DECISION_MADE,
        message="Synthetic audit",
        data={"reason_code": "TEST"},
        occurred_at=NOW,
        critical=False,
    )

    repo.append_audit_events([event, event])
    facts = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)

    assert [fact.fact_kind for fact in facts] == [FactKind.TRADE_AUDIT_RECORDED.value]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(BusinessAuditEventModel)) == 1


def test_ack_removes_only_fact_outbox_and_preserves_recovery_rows(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    repo.finalize_execution(filled_buy())
    facts = repo.ready_fact_outbox(20, now=NOW + timedelta(seconds=1), deadline_ms=0)

    repo.acknowledge_fact_outbox(
        automation_id,
        accepted_through_sequence=facts[-1].sequence_number,
        current_revision=2,
    )

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0
        assert session.scalar(select(func.count()).select_from(LocalIntentModel)) == 1
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == 1


def test_hold_is_a_typed_state_fact_and_excludes_automation_from_active_runtime(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)

    repo.hold_active("Synthetic restart", automation_id)
    facts = repo.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)

    assert repo.get_state(automation_id).state == AutomationState.HOLD.value
    assert repo.list_active() == []
    assert [fact.fact_kind for fact in facts] == [FactKind.AUTOMATION_STATE_CHANGED.value]


def test_invalid_worker_resume_keeps_state_revision_and_outbox_unchanged(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command().model_copy(update={"state": AutomationState.HOLD})
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)

    with pytest.raises(InvalidAutomationTransition):
        repo.transition_state(
            automation_id=automation_id,
            state=AutomationState.IN_WORK.value,
            safe_message="Synthetic invalid resume",
            occurred_at=NOW,
        )

    assert repo.get_state(automation_id).state == AutomationState.HOLD.value
    assert repo.get_state(automation_id).revision == command_value.revision
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def test_repeated_worker_state_is_a_noop_without_revision_or_fact(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)

    changed = repo.transition_state(
        automation_id=automation_id,
        state=AutomationState.IN_WORK.value,
        safe_message="Synthetic repeated activation",
        occurred_at=NOW,
    )

    assert changed is False
    assert repo.get_state(automation_id).revision == command_value.revision
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def test_worker_hold_requires_a_nonblank_reason_before_outbox_write(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)

    with pytest.raises(ValueError, match="hold reason"):
        repo.transition_state(
            automation_id=automation_id,
            state=AutomationState.HOLD.value,
            safe_message="  ",
            occurred_at=NOW,
        )

    assert repo.get_state(automation_id).state == AutomationState.IN_WORK.value
    assert repo.get_state(automation_id).revision == command_value.revision
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


@pytest.mark.parametrize("operation", ["transition", "hold_active", "bootstrap"])
def test_stale_worker_state_proposal_cannot_overwrite_authoritative_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / f'{operation}.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    command_value = baseline_command()
    if operation == "bootstrap":
        command_value = command_value.model_copy(
            update={
                "state": AutomationState.HOLD,
                "bootstrap": BrokerPositionBootstrap(
                    position_cycle_id=UUID("00000000-0000-4000-8000-000000000421"),
                    position_lot_id=UUID("00000000-0000-4000-8000-000000000422"),
                    quantity_lots=2,
                    average_price=Decimal("100"),
                    invested_amount=Decimal("2000"),
                    currency="RUB",
                    observed_at=NOW,
                ),
            }
        )
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    stale_loaded = Event()
    release_stale = Event()
    errors: list[BaseException] = []

    def pause_stale_load(_session: Session, instance: object) -> None:
        if current_thread().name != "stale-state-proposal":
            return
        if not isinstance(instance, CachedAutomationModel) or instance.automation_id != automation_id:
            return
        stale_loaded.set()
        if not release_stale.wait(timeout=2):
            raise TimeoutError("Synthetic stale proposal was not released")

    def propose_stale_state() -> None:
        try:
            if operation == "transition":
                repo.transition_state(
                    automation_id=automation_id,
                    state=AutomationState.HOLD.value,
                    safe_message="Synthetic stale transition",
                    occurred_at=NOW,
                )
            elif operation == "hold_active":
                repo.hold_active("Synthetic stale hold", automation_id)
            else:
                repo.ensure_position_bootstrap(command_value)
        except BaseException as error:
            errors.append(error)

    event.listen(Session, "loaded_as_persistent", pause_stale_load)
    thread = Thread(target=propose_stale_state, name="stale-state-proposal")
    try:
        thread.start()
        assert stale_loaded.wait(timeout=1)
        repo.reconcile_from_core(
            automation_id,
            state=AutomationState.CLOSED.value,
            revision=command_value.revision + 1,
            last_sequence_number=command_value.last_sequence_number,
        )
        release_stale.set()
        thread.join(timeout=2)
    finally:
        release_stale.set()
        thread.join(timeout=2)
        event.remove(Session, "loaded_as_persistent", pause_stale_load)

    assert len(errors) == 1
    assert isinstance(errors[0], InvalidAutomationTransition)
    state = repo.get_state(automation_id)
    assert state.state == AutomationState.CLOSED.value
    assert state.revision == command_value.revision + 1
    assert state.last_sequence_number == command_value.last_sequence_number
    with factory() as session:
        cached = session.get_one(CachedAutomationModel, automation_id)
        assert cached.position_cycle_id is None
        assert session.scalar(select(func.count()).select_from(TradeLotModel)) == 0
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def test_manual_resume_accepts_core_state_and_discards_failed_hold_fact(worker_repository_factory) -> None:
    repo, factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.hold_active("Synthetic runtime guard", automation_id)
    failed = repo.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)
    repo.reject_fact_outbox(
        automation_id,
        tuple(item.event_id for item in failed),
        reason="INVALID_FACT_STATE",
    )
    resumed = command_value.model_copy(
        update={
            "state": AutomationState.IN_QUEUE,
            "revision": 2,
            "last_sequence_number": 1,
        }
    )

    assert repo.cache_command(resumed) is True
    assert repo.get_state(automation_id).state == AutomationState.IN_QUEUE.value
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(FactOutboxModel)) == 0


def test_rejected_stale_fact_does_not_replace_authoritative_closed_state(worker_repository_factory) -> None:
    repo, _factory = worker_repository_factory()
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    repo.cache_command(command_value)
    repo.transition_state(
        automation_id=automation_id,
        state=AutomationState.HOLD.value,
        safe_message="Synthetic stale hold",
        occurred_at=NOW,
    )
    stale = repo.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    repo.reconcile_from_core(
        automation_id,
        state=AutomationState.CLOSED.value,
        revision=2,
        last_sequence_number=0,
    )

    repo.reject_fact_outbox(
        automation_id,
        tuple(item.event_id for item in stale),
        reason="INVALID_FACT_STATE",
    )

    assert repo.get_state(automation_id).state == AutomationState.CLOSED.value


@pytest.mark.parametrize("state", ["HOLD", "CLOSED", "IN_QUEUE"])
def test_stale_decision_cannot_create_intent_after_local_lifecycle_change(worker_repository_factory, state):
    repo, factory = worker_repository_factory()
    value = baseline_command()
    repo.cache_command(value)
    with factory.begin() as session:
        session.get_one(CachedAutomationModel, str(value.automation_id)).state = state
    result = repo.save_decision_batch((decision_item(value),), occurred_at=NOW)
    assert result.intents == ()
    assert result.decisions[0].decision == "WAIT"
    assert result.decisions[0].reason_code == "AUTOMATION_STATE_CHANGED"


def test_local_fact_revision_does_not_revoke_current_command(worker_repository_factory):
    repo, factory = worker_repository_factory()
    value = baseline_command()
    repo.cache_command(value)
    with factory.begin() as session:
        session.get_one(CachedAutomationModel, str(value.automation_id)).revision += 3
    assert len(repo.save_decision_batch((decision_item(value),), occurred_at=NOW).intents) == 1


def test_hold_after_dispatch_keeps_committed_order_supervision(tmp_path):

    engine = create_worker_engine(f"sqlite:///{tmp_path / 'supervision.db'}")
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = baseline_command()
    repo.cache_command(value)
    item = decision_item(value)
    request = DispatchRequest(
        INTENT_ID,
        value.account_id,
        value.external_instrument_id,
        OrderSide.BUY,
        1,
        Decimal(100),
        automation_id=str(value.automation_id),
        lot_size=value.lot_size,
        broker_id=str(value.broker_id),
        process_id=item.process_id,
        reservation_currency="RUB",
        required_cash=Decimal(1001),
    )

    async def scenario():
        release, sent = asyncio.Event(), asyncio.Event()

        class Broker:
            async def dispatch_limit_order(self, request):
                sent.set()
                await release.wait()
                return BrokerOrderState(
                    "order",
                    INTENT_ID,
                    "FILLED",
                    1,
                    1,
                    Decimal(1000),
                    Decimal(1000),
                    Decimal(1),
                    Decimal(2),
                    "RUB",
                    executed_price=Decimal(100),
                    executed_at=NOW,
                )

        tracking = OrderTrackingService(repo, now=lambda: NOW, broker_id=str(value.broker_id))
        batch = BatchTradingRuntimeService(
            repo,
            OrderDispatchService(repo, Broker(), now=lambda: NOW, access_mode="TRADE"),
            tracking,
            now=lambda: NOW,
            access_mode="TRADE",
        )
        result = await batch.run_batch((item,), (request,), snapshot_at=NOW)
        assert len(result.persisted.intents) == 1
        await sent.wait()
        repo.hold_active("Control HOLD", str(value.automation_id))
        release.set()
        await tracking.wait_all()
        assert repo.get_active_intent(str(value.automation_id)) is None
        assert len(repo.list_open_lots(str(value.automation_id))) == 1
        assert repo.get_state(str(value.automation_id)).state == "HOLD"

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_decision_admission_serializes_hold_committed_by_another_connection(tmp_path):
    """A HOLD writer cannot overtake an admitted intent between its read and write."""
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'admission-race.db'}")
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = baseline_command()
    repo.cache_command(value)
    loaded, release, hold_attempted, hold_finished = Event(), Event(), Event(), Event()
    errors = []
    result = []

    def pause_loaded(session, instance):
        if current_thread().name == "admission" and isinstance(instance, CachedAutomationModel):
            loaded.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Admission barrier timed out")

    def observe_hold_write(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name == "hold" and statement.lstrip().upper().startswith("UPDATE CACHED_AUTOMATIONS"):
            hold_attempted.set()

    def admit():
        try:
            result.append(repo.save_decision_batch((decision_item(value),), occurred_at=NOW))
        except BaseException as error:
            errors.append(error)

    def hold():
        try:
            repo.hold_active("Concurrent control HOLD", str(value.automation_id))
        except BaseException as error:
            errors.append(error)
        finally:
            hold_finished.set()

    event.listen(Session, "loaded_as_persistent", pause_loaded)
    event.listen(engine, "before_cursor_execute", observe_hold_write)
    admission, control = Thread(target=admit, name="admission"), Thread(target=hold, name="hold")
    try:
        admission.start()
        assert loaded.wait(timeout=1)
        control.start()
        assert hold_attempted.wait(timeout=1)
        assert not hold_finished.wait(timeout=0.2), "HOLD overtook admission after its state read"
        release.set()
        admission.join(timeout=2)
        control.join(timeout=2)
        assert not admission.is_alive()
        assert not control.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], InvalidAutomationTransition)
        assert len(result[0].intents) == 1
        assert repo.get_state(str(value.automation_id)).state == "IN_WORK"
        assert repo.get_active_intent(str(value.automation_id)) is not None
        # The stale lifecycle proposal preserves the existing CAS contract.
        # A fresh HOLD observes the committed intent and keeps it supervised.
        repo.hold_active("Retry control HOLD after admission", str(value.automation_id))
        assert repo.get_state(str(value.automation_id)).state == "HOLD"
        assert repo.get_active_intent(str(value.automation_id)) is not None
    finally:
        release.set()
        admission.join(timeout=3)
        if control.ident is not None:
            control.join(timeout=3)
        event.remove(Session, "loaded_as_persistent", pause_loaded)
        event.remove(engine, "before_cursor_execute", observe_hold_write)
        engine.dispose()


def test_hold_transaction_started_first_prevents_stale_decision_intent(tmp_path):
    """Admission waits for an earlier HOLD transaction and reads its committed state."""
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'hold-first.db'}")
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = baseline_command()
    repo.cache_command(value)
    hold_written, release_hold, admission_started, cached_loaded = Event(), Event(), Event(), Event()
    errors, results = [], []

    def pause_hold_commit(session):
        if current_thread().name == "hold-first":
            hold_written.set()
            if not release_hold.wait(timeout=3):
                raise TimeoutError("HOLD commit barrier timed out")

    def observe_loaded(session, instance):
        if current_thread().name == "admission-second" and isinstance(instance, CachedAutomationModel):
            cached_loaded.set()

    def hold():
        try:
            repo.hold_active("Concurrent HOLD first", str(value.automation_id))
        except BaseException as error:
            errors.append(error)

    def admit():
        admission_started.set()
        try:
            results.append(repo.save_decision_batch((decision_item(value),), occurred_at=NOW))
        except BaseException as error:
            errors.append(error)

    event.listen(Session, "before_commit", pause_hold_commit)
    event.listen(Session, "loaded_as_persistent", observe_loaded)
    control = Thread(target=hold, name="hold-first")
    admission = Thread(target=admit, name="admission-second")
    try:
        control.start()
        assert hold_written.wait(timeout=1)
        admission.start()
        assert admission_started.wait(timeout=1)
        # The old SQLite read path loads IN_WORK while HOLD is still uncommitted.
        cached_loaded.wait(timeout=0.2)
        release_hold.set()
        control.join(timeout=2)
        admission.join(timeout=2)
        assert not control.is_alive()
        assert not admission.is_alive()
        assert errors == []
        assert results[0].intents == ()
        assert results[0].decisions[0].reason_code == "AUTOMATION_STATE_CHANGED"
        assert repo.get_state(str(value.automation_id)).state == "HOLD"
    finally:
        release_hold.set()
        control.join(timeout=3)
        if admission.ident is not None:
            admission.join(timeout=3)
        event.remove(Session, "before_commit", pause_hold_commit)
        event.remove(Session, "loaded_as_persistent", observe_loaded)
        engine.dispose()
