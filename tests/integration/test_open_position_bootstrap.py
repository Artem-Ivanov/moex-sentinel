"""End-to-end bootstrap from Core HOLD snapshot through Worker typed facts."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.models import (
    AutomationEventModel,
    BrokerOrderModel,
    PositionCycleModel,
    PositionLotModel,
    TradeExecutionModel,
    TradingAutomationModel,
)
from moex_sentinel.storage.models import (
    Base as CoreBase,
)
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    BrokerPositionBootstrap,
    FactEnvelope,
    FactIngressErrorCode,
    PositionLotSource,
)
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.analytics_runtime_helpers import build_analytics_runtime
from tests.trading_automaton.command_factory import command
from tests.trading_automaton.services.test_broker_tick_preparation_service import Hydration, Portfolio
from tests.trading_automaton.test_analytics_runtime import FALLBACK, Source, Tick, frame
from tests.trading_automaton.test_analytics_runtime import NOW as MARKET_NOW
from trading_automaton.config import StrategySettings
from trading_automaton.services.analytics_frame import AnalyticsMetricsCache
from trading_automaton.services.broker_tick_preparation import BrokerTickPreparationService
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 14, 10, 0, 0, 123000, tzinfo=UTC)
SCOPE_ID = UUID("00000000-0000-4000-8000-000000000601")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000602")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000603")
CYCLE_ID = UUID("00000000-0000-4000-8000-000000000604")
LOT_ID = UUID("00000000-0000-4000-8000-000000000605")


def bootstrap_context(*, core_state=AutomationState.HOLD, core_revision=1, prepare_worker=True, worker_db_path=None):
    core_engine = create_engine("sqlite:///:memory:")
    CoreBase.metadata.create_all(core_engine)
    core_factory = sessionmaker(core_engine, expire_on_commit=False)
    with core_factory.begin() as session:
        session.add(user_broker_model(str(SCOPE_ID), "account-1"))
        session.add(instrument_model(str(INSTRUMENT_ID), str(SCOPE_ID)))
        session.add(
            TradingAutomationModel(
                id=str(AUTOMATION_ID),
                user_broker_id=str(SCOPE_ID),
                instrument_id=str(INSTRUMENT_ID),
                state=core_state.value,
                suspended_from_state=None,
                hold_reason="BOOTSTRAPPING",
                revision=core_revision,
                last_sequence_number=0,
                resume_requested=False,
                closed_at=None,
                bootstrap_position_cycle_id=str(CYCLE_ID),
                bootstrap_position_lot_id=str(LOT_ID),
                bootstrap_quantity_lots=2,
                bootstrap_average_price=Decimal("100"),
                bootstrap_invested_amount=Decimal("2000"),
                bootstrap_currency="RUB",
                bootstrap_observed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    worker_engine = create_engine("sqlite:///:memory:" if worker_db_path is None else f"sqlite:///{worker_db_path}")
    WorkerBase.metadata.create_all(worker_engine)
    worker = LocalAutomationRepository(
        sessionmaker(worker_engine, expire_on_commit=False),
        fact_writer=FactOutboxWriter(clock=lambda: NOW),
    )
    value = command(
        automation=str(AUTOMATION_ID),
        broker=str(SCOPE_ID),
        account="account-1",
        instrument="external-instrument",
        state=core_state,
        bootstrap=BrokerPositionBootstrap(
            position_cycle_id=CYCLE_ID,
            position_lot_id=LOT_ID,
            quantity_lots=2,
            average_price=Decimal("100"),
            invested_amount=Decimal("2000"),
            currency="RUB",
            observed_at=NOW,
        ),
    ).model_copy(
        update={
            "automation_id": AUTOMATION_ID,
            "user_broker_id": SCOPE_ID,
            "broker_id": SCOPE_ID,
            "instrument_id": INSTRUMENT_ID,
        }
    )
    if prepare_worker:
        worker.cache_command(value)
        PositionBootstrapService(worker).ensure(value, StrategySettings())
    rows = worker.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    adapter: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)
    facts = [
        adapter.validate_python(
            {
                "event_id": row.event_id,
                "user_broker_id": row.user_broker_id,
                "automation_id": row.automation_id,
                "sequence_number": row.sequence_number,
                "expected_revision": row.expected_revision,
                "fact_kind": row.fact_kind,
                "payload": row.payload,
                "safe_message": row.safe_message,
                "occurred_at": row.occurred_at,
            }
        )
        for row in rows
    ]
    ingress = TradingFactIngressService(
        lambda: TradingFactsUnitOfWork(core_factory),
        TradingFactMapper(),
        now=lambda: NOW,
    )
    return core_factory, worker, ingress, facts


@pytest.mark.parametrize("market_problem", ["unavailable", "stale", "closed", "crossed"])
def test_unavailable_market_allows_position_adoption_but_no_tick_or_order(tmp_path, market_problem) -> None:
    core_factory, worker, ingress, _ = bootstrap_context(
        core_state=AutomationState.IN_QUEUE, prepare_worker=False, worker_db_path=tmp_path / "worker.db"
    )
    commands = AutomationCommandRepository(core_factory)
    value = commands.claim(10)[0]
    assert worker.cache_command(value)
    source_frame = frame(at=MARKET_NOW, book_at=MARKET_NOW, ids=(value.external_instrument_id,))
    instrument = source_frame.instruments[0]
    market = instrument.market
    if market_problem == "stale":
        market = market.model_copy(
            update={
                "order_book": market.order_book.model_copy(update={"captured_at": MARKET_NOW - timedelta(seconds=3)})
            }
        )
    elif market_problem == "closed":
        market = market.model_copy(
            update={"trading_status": market.trading_status.model_copy(update={"api_trade_available": False})}
        )
    elif market_problem == "crossed":
        market = market.model_copy(
            update={"order_book": market.order_book.model_copy(update={"asks": market.order_book.bids})}
        )
    source_frame = source_frame.model_copy(update={"instruments": (instrument.model_copy(update={"market": market}),)})

    class Broker:
        async def get_positions(self, account_id):
            return (BrokerPosition(value.external_instrument_id, Decimal(2), Decimal(100), Decimal(101), "RUB"),)

    class NoCommission:
        async def refresh_if_due(self, *args, **kwargs):
            raise AssertionError("Adoption without market must not estimate an order commission")

    async def scenario():
        tick = Tick()
        preparation = BrokerTickPreparationService(
            Broker(),
            Portfolio(),
            None,
            NoCommission(),
            Hydration(),
            position_bootstrap=PositionBootstrapService(worker),
            now=lambda: MARKET_NOW,
        )
        runtime = build_analytics_runtime(
            Source(
                httpx.ReadTimeout("offline analytics unavailable") if market_problem == "unavailable" else source_frame
            ),
            tick,
            preparation=preparation,
            metrics=AnalyticsMetricsCache(),
            source_id=str(SCOPE_ID),
            fallback=FALLBACK,
            now=lambda: MARKET_NOW,
        )
        await runtime.replace_commands((value,))
        await runtime.run_once()
        facts = [
            FactSynchronizationService._envelope(row)
            for row in worker.ready_fact_outbox(10, now=MARKET_NOW, deadline_ms=0)
        ]
        result = ingress.publish(facts)
        assert result.failures == ()
        assert result.results[0].accepted_through_sequence == 4
        worker.acknowledge_fact_outbox(str(AUTOMATION_ID), accepted_through_sequence=4, current_revision=2)
        await runtime.replace_commands((value.model_copy(update={"state": AutomationState.IN_WORK}),))
        await runtime.run_once()
        await runtime.close()
        assert tick.calls == []
        assert worker.get_active_intent(str(AUTOMATION_ID)) is None

    asyncio.run(scenario())
    with core_factory() as session:
        assert session.get_one(TradingAutomationModel, str(AUTOMATION_ID)).state == "IN_WORK"
        assert session.get_one(PositionCycleModel, str(CYCLE_ID)).quantity_lots == 2
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
        assert session.scalar(select(func.count()).select_from(BrokerOrderModel)) == 0
        assert session.scalar(select(func.count()).select_from(TradeExecutionModel)) == 0


def test_resumed_unfinished_bootstrap_recovers_speculative_activation_and_reconciled_lot() -> None:
    core_factory, worker, ingress, _facts = bootstrap_context(
        core_state=AutomationState.IN_QUEUE, core_revision=2, prepare_worker=False
    )
    commands = AutomationCommandRepository(core_factory)
    current = commands.claim(10)[0]
    # Reproduce the old Worker receiving a command with its bootstrap omitted.
    legacy = current.model_copy(update={"bootstrap": None})
    assert worker.cache_command(legacy)
    worker.create_trade_lot(
        automation_id=str(AUTOMATION_ID),
        source_intent_id=None,
        source="RECONCILED",
        quantity_lots=2,
        entry_price=Decimal("100"),
        entry_commission=Decimal(),
        opened_at=NOW,
    )
    worker.transition_state(
        automation_id=str(AUTOMATION_ID),
        state="IN_WORK",
        safe_message="Legacy activation without bootstrap",
        occurred_at=NOW,
    )
    speculative = worker.list_active()[0]
    assert (speculative.state, speculative.revision, speculative.last_sequence_number) == (
        AutomationState.IN_WORK,
        3,
        1,
    )

    class OfflineCore:
        def claim_commands(self, worker_id, limit):
            return commands.claim(limit)

        def automation_statuses(self, automation_ids):
            return commands.statuses(automation_ids)

        def publish_facts(self, facts):
            return ingress.publish(facts)

    synchronization = FactSynchronizationService(
        worker, OfflineCore(), now=lambda: NOW, sleep=lambda _: None, deadline_ms=0
    )
    assert synchronization.flush_outbox()
    assert worker.get_state(str(AUTOMATION_ID)).state == "HOLD"
    recovered = synchronization.claim_commands("offline-worker", 10)[0]
    assert recovered.bootstrap is not None
    assert PositionBootstrapService(worker).ensure(recovered, StrategySettings()).applied
    assert synchronization.flush_outbox()
    assert not PositionBootstrapService(worker).ensure(recovered, StrategySettings()).applied

    lots = worker.list_open_lots(str(AUTOMATION_ID))
    assert [(lot.id, lot.original_lots, lot.remaining_lots, lot.source) for lot in lots] == [
        (str(LOT_ID), 2, 2, "BROKER_POSITION_BOOTSTRAP")
    ]
    assert worker.ready_fact_outbox(10, now=NOW, deadline_ms=0) == []
    with core_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
        assert (automation.state, automation.revision, automation.last_sequence_number) == ("IN_WORK", 3, 4)
        assert session.get_one(PositionCycleModel, str(CYCLE_ID)).quantity_lots == 2
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
        assert session.scalar(select(func.count()).select_from(BrokerOrderModel)) == 0
        assert session.scalar(select(func.count()).select_from(TradeExecutionModel)) == 0


@pytest.mark.parametrize("state", [AutomationState.HOLD, AutomationState.IN_QUEUE])
def test_adopted_position_reaches_in_work_only_after_atomic_core_fact_group(state: AutomationState) -> None:
    core_factory, _worker, ingress, facts = bootstrap_context()
    with core_factory.begin() as session:
        session.get_one(TradingAutomationModel, str(AUTOMATION_ID)).state = state.value

    result = ingress.publish(facts)

    assert result.failures == ()
    assert result.results[0].accepted_through_sequence == 4
    assert result.results[0].current_revision == 2
    with core_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
        cycle = session.get_one(PositionCycleModel, str(CYCLE_ID))
        lot = session.get_one(PositionLotModel, str(LOT_ID))
        assert automation.state == AutomationState.IN_WORK.value
        assert cycle.quantity_lots == 2
        assert cycle.average_entry_price == Decimal("100")
        assert cycle.realized_pnl == cycle.accumulated_commissions == 0
        assert lot.source == PositionLotSource.BROKER_POSITION_BOOTSTRAP.value
        assert lot.buy_execution_id is None
        assert session.scalar(select(func.count()).select_from(BrokerOrderModel)) == 0
        assert session.scalar(select(func.count()).select_from(TradeExecutionModel)) == 0


@pytest.mark.parametrize("prefix_size", [1, 2, 3])
def test_core_rejects_incomplete_bootstrap_without_partial_position(prefix_size: int) -> None:
    core_factory, _worker, ingress, facts = bootstrap_context()

    result = ingress.publish(facts[:prefix_size])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE
    with core_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
        assert automation.state == AutomationState.HOLD.value
        assert automation.last_sequence_number == 0
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 0
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 0
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 0


def test_core_rejects_bootstrap_activation_without_position_facts() -> None:
    _core_factory, _worker, ingress, facts = bootstrap_context()
    activation = facts[-1].model_copy(update={"sequence_number": 1})

    result = ingress.publish([activation])

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE


def test_core_rejects_bootstrap_values_that_differ_from_authoritative_snapshot() -> None:
    _core_factory, _worker, ingress, facts = bootstrap_context()
    facts[0] = facts[0].model_copy(update={"payload": facts[0].payload.model_copy(update={"quantity_lots": 3})})

    result = ingress.publish(facts)

    assert result.results == ()
    assert result.failures[0].code is FactIngressErrorCode.INVALID_FACT_STATE


def test_complete_bootstrap_retry_is_idempotent_after_a_lost_acknowledgement() -> None:
    core_factory, worker, ingress, facts = bootstrap_context()
    assert ingress.publish(facts).failures == ()

    retried = ingress.publish(facts)

    assert retried.failures == ()
    assert retried.results[0].accepted_through_sequence == 4
    worker.acknowledge_fact_outbox(
        str(AUTOMATION_ID),
        accepted_through_sequence=4,
        current_revision=2,
    )
    assert worker.ready_fact_outbox(1, now=NOW, deadline_ms=0) == []
    with core_factory() as session:
        assert session.scalar(select(func.count()).select_from(PositionCycleModel)) == 1
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
        assert session.scalar(select(func.count()).select_from(AutomationEventModel)) == 4


def test_bootstrap_publication_leaves_a_later_hold_for_the_next_core_transaction() -> None:
    core_factory, worker, ingress, facts = bootstrap_context()
    worker.transition_state(
        automation_id=str(AUTOMATION_ID),
        state="HOLD",
        safe_message="Synthetic runtime hold",
        occurred_at=NOW,
    )

    initial = worker.ready_fact_outbox(10, now=NOW, deadline_ms=0)

    assert [row.sequence_number for row in initial] == [1, 2, 3, 4]
    assert ingress.publish(facts).failures == ()
    worker.acknowledge_fact_outbox(str(AUTOMATION_ID), accepted_through_sequence=4, current_revision=2)
    remaining = worker.ready_fact_outbox(10, now=NOW, deadline_ms=0)
    assert [(row.sequence_number, row.expected_revision) for row in remaining] == [(5, 2)]
    adapter: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)
    hold = adapter.validate_python(
        remaining[0].model_dump(
            include={
                "event_id",
                "user_broker_id",
                "automation_id",
                "sequence_number",
                "expected_revision",
                "fact_kind",
                "payload",
                "safe_message",
                "occurred_at",
            }
        )
    )
    assert ingress.publish([hold]).failures == ()
    with core_factory() as session:
        automation = session.get_one(TradingAutomationModel, str(AUTOMATION_ID))
        assert automation.state == AutomationState.HOLD.value
        assert automation.revision == 3
        assert session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
