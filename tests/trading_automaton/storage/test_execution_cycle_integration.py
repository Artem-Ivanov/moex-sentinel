"""Execution and scalping cycle share one durable transaction."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from moex_sentinel.domain.market_data import HistoricCandle
from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel, OrderSide
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import PositionLotOpenedPayload
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.worker_storage_helpers import (
    INTENT_ID,
    NOW,
    SELL_INTENT_ID,
    baseline_command,
    filled_buy,
)
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import CommissionSchedule, DispatchRequest, PositionWorkItem
from trading_automaton.services.batch_runtime import BatchTradingRuntimeService
from trading_automaton.services.decision import TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.position_state_hydration import PositionStateCacheService, PositionStateHydrationService
from trading_automaton.services.streaming_cycle_transition import StreamingCycleTransitionService
from trading_automaton.services.streaming_position_decision import StreamingPositionDecisionService
from trading_automaton.services.trading_cycle import TradingCycleService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, LocalIntentModel, TradingCycleStateModel
from trading_automaton.storage.repository import IntentBatchItem, LocalAutomationRepository, TradingCycleState

CANDLE_AT = NOW - timedelta(minutes=1)


@pytest.fixture
def storage(tmp_path):
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'cycle.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, fact_writer=FactOutboxWriter(clock=lambda: NOW))
    repo.cache_command(baseline_command())
    try:
        yield repo, factory, engine
    finally:
        engine.dispose()


def seed_buy(repo, *, metadata=True, lots=1):
    item = decision_item(baseline_command()).model_copy(
        update={
            "indicators": {"last_candle_at": CANDLE_AT.isoformat()} if metadata else {},
            "intent": IntentBatchItem(INTENT_ID, "BUY_MORE", "BUY", lots, Decimal("100")),
            "decision_quantity_lots": lots,
        }
    )
    repo.save_decision_batch((item,), occurred_at=NOW)
    return repo.save_cycle_state(
        TradingCycleState(str(baseline_command().automation_id), Decimal("98"), None, True, None, NOW)
    )


@pytest.mark.parametrize("terminal", ["FILLED", "CANCELLED", "REJECTED"])
def test_nonzero_buy_persists_decision_candle_after_reopening(storage, terminal):
    repo, factory, engine = storage
    before = seed_buy(repo, lots=2)
    result = repo.finalize_execution(filled_buy().model_copy(update={"state": terminal, "quantity_lots": 2}))
    engine.dispose()

    reopened = LocalAutomationRepository(factory)
    cycle = reopened.get_cycle_state(before.automation_id, now=NOW)
    assert result.applied
    assert cycle.pending_low is None
    assert cycle.last_buy_candle_at == CANDLE_AT
    assert cycle.updated_at == filled_buy().occurred_at
    assert len(reopened.list_open_lots(before.automation_id)) == 1


def test_exact_replay_preserves_later_cycle_observation(storage):
    repo, _, _ = storage
    before = seed_buy(repo)
    repo.finalize_execution(filled_buy())
    observed = repo.get_cycle_state(before.automation_id, now=NOW).model_copy(
        update={"pending_low": Decimal("97"), "updated_at": NOW + timedelta(seconds=1)}
    )
    repo.save_cycle_state(observed)
    facts_before = repo.ready_fact_outbox(100, now=NOW + timedelta(seconds=2), deadline_ms=0)

    replay = repo.finalize_execution(filled_buy())

    assert not replay.applied
    assert repo.get_cycle_state(before.automation_id, now=NOW) == observed
    assert repo.ready_fact_outbox(100, now=NOW + timedelta(seconds=2), deadline_ms=0) == facts_before


@pytest.mark.parametrize("terminal", ["CANCELLED", "REJECTED"])
def test_zero_fill_does_not_change_cycle(storage, terminal):
    repo, _, _ = storage
    before = seed_buy(repo)
    repo.finalize_execution(
        filled_buy().model_copy(
            update={"state": terminal, "executed_lots": 0, "executed_amount": Decimal(), "executed_price": Decimal()}
        )
    )
    assert repo.get_cycle_state(before.automation_id, now=NOW) == before
    assert repo.list_open_lots(before.automation_id) == []


@pytest.mark.parametrize("existing_marker", [None, NOW + timedelta(minutes=1)])
@pytest.mark.parametrize("executed_at", [NOW + timedelta(seconds=35), None])
def test_legacy_buy_uses_execution_minute_without_moving_marker_back(storage, existing_marker, executed_at):
    repo, _, _ = storage
    before = seed_buy(repo, metadata=False)
    repo.save_cycle_state(before.model_copy(update={"last_buy_candle_at": existing_marker}))
    repo.finalize_execution(filled_buy().model_copy(update={"executed_at": executed_at}))

    cycle = repo.get_cycle_state(before.automation_id, now=NOW)
    assert cycle.last_buy_candle_at == (existing_marker or NOW)
    assert cycle.pending_low is None


@pytest.mark.parametrize("terminal", ["FILLED", "CANCELLED", "REJECTED"])
def test_nonzero_sell_disarms_cycle_including_partial_terminal_fill(storage, terminal):
    repo, _, _ = storage
    before = seed_buy(repo, lots=2)
    repo.finalize_execution(filled_buy().model_copy(update={"quantity_lots": 2, "executed_lots": 2}))
    repo.save_cycle_state(
        repo.get_cycle_state(before.automation_id, now=NOW).model_copy(update={"pending_low": Decimal("99")})
    )
    item = decision_item(baseline_command(), intent_id=SELL_INTENT_ID).model_copy(
        update={
            "decision": "SELL_PART",
            "intent": IntentBatchItem(SELL_INTENT_ID, "SELL_PART", "SELL", 2, Decimal("100")),
            "decision_quantity_lots": 2,
        }
    )
    repo.save_decision_batch((item,), occurred_at=NOW + timedelta(seconds=1))
    repo.finalize_execution(
        filled_buy().model_copy(
            update={
                "intent_id": SELL_INTENT_ID,
                "side": "SELL",
                "state": terminal,
                "quantity_lots": 2,
                "executed_price": Decimal("110"),
                "occurred_at": NOW + timedelta(seconds=2),
            }
        )
    )
    cycle = repo.get_cycle_state(before.automation_id, now=NOW)
    assert not cycle.sell_armed
    assert cycle.last_sell_price == Decimal("110")
    assert cycle.pending_low is None
    assert cycle.last_buy_candle_at == CANDLE_AT
    assert sum(lot.remaining_lots for lot in repo.list_open_lots(before.automation_id)) == 1


class FailAfterExecutionFact(FactOutboxWriter):
    def append(self, session, cached, **values):
        result = super().append(session, cached, **values)
        if isinstance(values["payload"], PositionLotOpenedPayload):
            raise RuntimeError("Synthetic outbox failure")  # noqa: TRY004 - injected failure, not a type error
        return result


def test_outbox_failure_rolls_back_execution_and_cycle(storage):
    repo, factory, _ = storage
    before = seed_buy(repo)
    failing = LocalAutomationRepository(factory, fact_writer=FailAfterExecutionFact(clock=lambda: NOW))
    with pytest.raises(RuntimeError, match="Synthetic outbox failure"):
        failing.finalize_execution(filled_buy())

    assert repo.get_cycle_state(before.automation_id, now=NOW) == before
    assert repo.list_open_lots(before.automation_id) == []
    with factory() as session:
        assert session.get_one(LocalIntentModel, INTENT_ID).terminal_at is None
    assert len(repo.ready_fact_outbox(100, now=NOW + timedelta(seconds=1), deadline_ms=0)) == 2


def test_buy_initializes_missing_cycle(storage):
    repo, factory, _ = storage
    repo.save_decision_batch((decision_item(baseline_command()),), occurred_at=NOW)
    repo.finalize_execution(filled_buy())
    with factory() as session:
        cycle = session.scalar(select(TradingCycleStateModel))
        assert cycle is not None
        assert cycle.last_buy_candle_at == NOW


def test_stale_wait_cannot_erase_committed_buy_cycle(storage):
    repo, _, _ = storage
    before = seed_buy(repo)
    repo.finalize_execution(filled_buy())
    committed = repo.get_cycle_state(before.automation_id, now=NOW)
    stale = decision_item(baseline_command()).model_copy(
        update={
            "decision": "WAIT",
            "intent": None,
            "cycle_state": before.model_copy(update={"updated_at": NOW + timedelta(seconds=1)}).model_dump(),
        }
    )
    repo.save_decision_batch((stale,), occurred_at=NOW + timedelta(seconds=1))

    assert repo.get_cycle_state(before.automation_id, now=NOW) == committed


@pytest.mark.parametrize(("baseline", "newer_observation"), [(False, False), (True, False), (True, True)])
def test_stale_sell_snapshot_preserves_execution_but_fresh_observation_can_rearm(storage, baseline, newer_observation):
    repo, _, _ = storage
    before = seed_buy(repo, lots=2)
    repo.finalize_execution(filled_buy().model_copy(update={"quantity_lots": 2, "executed_lots": 2}))
    loaded = repo.get_cycle_state(before.automation_id, now=NOW)
    sell_item = decision_item(baseline_command(), intent_id=SELL_INTENT_ID).model_copy(
        update={
            "decision": "SELL_PART",
            "intent": IntentBatchItem(SELL_INTENT_ID, "SELL_PART", "SELL", 1, Decimal("100")),
        }
    )
    repo.save_decision_batch((sell_item,), occurred_at=NOW + timedelta(seconds=1))
    repo.finalize_execution(
        filled_buy().model_copy(
            update={
                "intent_id": SELL_INTENT_ID,
                "side": "SELL",
                "executed_price": Decimal("110"),
                "occurred_at": NOW + timedelta(seconds=2),
            }
        )
    )
    committed = repo.get_cycle_state(before.automation_id, now=NOW)
    cycle_state = loaded.model_dump()
    if newer_observation:
        cycle_state["updated_at"] = NOW + timedelta(seconds=3)
    if baseline:
        cycle_state["expected_state"] = loaded.model_dump()
    stale = decision_item(baseline_command()).model_copy(
        update={"decision": "WAIT", "intent": None, "cycle_state": cycle_state}
    )
    repo.save_decision_batch((stale,), occurred_at=NOW + timedelta(seconds=3))

    assert repo.get_cycle_state(before.automation_id, now=NOW) == committed
    fresh_cycle = committed.model_copy(update={"sell_armed": True, "updated_at": NOW + timedelta(seconds=4)})
    fresh_item = stale.model_copy(
        update={"cycle_state": {**fresh_cycle.model_dump(), "expected_state": committed.model_dump()}}
    )
    repo.save_decision_batch((fresh_item,), occurred_at=NOW + timedelta(seconds=4))
    assert repo.get_cycle_state(before.automation_id, now=NOW) == fresh_cycle


class ForbiddenDispatch:
    async def dispatch(self, request, started):
        raise AssertionError("A stale cycle must not reach broker dispatch")

    def track(self, intent_id, task, *, request=None):
        raise AssertionError("A stale cycle must not start tracking")


def test_stale_actionable_cycle_does_not_create_intent_or_dispatch(storage):
    repo, _, _ = storage
    before = seed_buy(repo)
    repo.finalize_execution(filled_buy())
    stale_cycle = {**before.model_dump(), "expected_state": before.model_dump()}
    stale_item = decision_item(baseline_command(), intent_id=SELL_INTENT_ID).model_copy(
        update={"cycle_state": stale_cycle}
    )
    request = DispatchRequest(
        SELL_INTENT_ID,
        stale_item.account_id,
        stale_item.instrument_id,
        OrderSide.BUY,
        1,
        Decimal("100"),
        instrument_type=stale_item.instrument_type,
        automation_id=stale_item.automation_id,
        lot_size=stale_item.lot_size,
        process_id=stale_item.process_id,
        broker_id=stale_item.broker_id,
        reservation_currency="RUB",
        required_cash=Decimal("1001"),
    )
    runtime = BatchTradingRuntimeService(
        repo, ForbiddenDispatch(), ForbiddenDispatch(), now=lambda: NOW + timedelta(seconds=3)
    )
    result = asyncio.run(runtime.run_batch((stale_item,), (request,), snapshot_at=NOW))
    persisted = result.persisted

    assert persisted.intents == ()
    assert persisted.decisions[0].decision == "WAIT"
    assert persisted.decisions[0].reason_code == "CYCLE_STATE_CHANGED"
    assert result.sla == ()
    facts = repo.ready_fact_outbox(100, now=NOW + timedelta(seconds=4), deadline_ms=0)
    assert facts[-1].payload["reason_code"] == "CYCLE_STATE_CHANGED"


def test_cycle_compare_and_set_normalizes_hot_timestamp_to_database_precision(storage):
    repo, _, _ = storage
    before = seed_buy(repo)
    hot_state = before.model_copy(update={"updated_at": NOW + timedelta(microseconds=123456)})
    repo.save_cycle_state(hot_state)
    observed = hot_state.model_copy(update={"pending_low": Decimal("97"), "updated_at": NOW + timedelta(seconds=1)})
    item = decision_item(baseline_command()).model_copy(
        update={
            "decision": "WAIT",
            "intent": None,
            "cycle_state": {**observed.model_dump(), "expected_state": hot_state.model_dump()},
        }
    )
    repo.save_decision_batch((item,), occurred_at=NOW + timedelta(seconds=1))
    assert repo.get_cycle_state(before.automation_id, now=NOW) == observed


class CompletedCandles:
    latest = CANDLE_AT

    async def completed(self, instrument_id):
        return tuple(
            HistoricCandle(
                instrument_id,
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                10,
                self.latest - timedelta(minutes=offset),
                True,
            )
            for offset in reversed(range(20))
        )


class FilledPortfolio:
    async def position(self, account_id, instrument_id):
        return BrokerPosition(instrument_id, Decimal("1"), Decimal("100"), Decimal("99.3"), "RUB")


class CommissionProfile:
    def schedule(self, key, *, snapshot_at):
        return CommissionSchedule(Decimal("0.001"), Decimal("0.001"))


class AvailableCash:
    async def available(self, account_id, currency):
        return Decimal("10000")

    async def reserved(self, account_id, currency):
        return Decimal()


def test_rehydrated_fill_blocks_same_candle_then_allows_next_candle_reversal(storage):
    repo, factory, engine = storage
    before = seed_buy(repo)
    repo.finalize_execution(filled_buy())
    engine.dispose()
    reopened = LocalAutomationRepository(factory)
    command = baseline_command()

    async def scenario():
        cache = PositionStateCacheService()
        candles = CompletedCandles()
        hydration = PositionStateHydrationService(reopened, FilledPortfolio(), candles, cache, now=lambda: NOW)
        await hydration.hydrate((command,))
        assert await cache.get(before.automation_id) is None
        facts = reopened.ready_fact_outbox(100, now=NOW + timedelta(seconds=1), deadline_ms=0)
        reopened.acknowledge_fact_outbox(
            before.automation_id, accepted_through_sequence=facts[-1].sequence_number, current_revision=1
        )
        await hydration.hydrate((command,))
        state = await cache.get(before.automation_id)
        assert state is not None
        assert not state.has_active_intent
        assert state.cycle.last_buy_candle_at == CANDLE_AT

        transitions = StreamingCycleTransitionService(
            now=lambda: NOW, cycles=TradingCycleService(), order_books=OrderBookValidationService()
        )
        evaluator = StreamingPositionDecisionService(
            CommissionProfile(),
            cash=AvailableCash(),
            decisions=TradeDecisionService(),
            contexts=DecisionContextService(StrategySettings()),
        )

        def market(price):
            return InstrumentMarketState(
                command.external_instrument_id,
                StreamOrderBook(
                    command.external_instrument_id,
                    (OrderBookLevel(Decimal(price), 10),),
                    (OrderBookLevel(Decimal(price) + Decimal("0.01"), 10),),
                    NOW,
                    True,
                ),
            )

        observed = transitions.apply(command, state, market("99"))
        rebound = transitions.apply(command, observed, market("99.3"))
        same_candle = await evaluator.decide(
            PositionWorkItem(command, False, state=rebound, snapshot_at=NOW), market("99.3")
        )
        reopened.save_cycle_state(rebound.cycle)
        candles.latest += timedelta(minutes=1)
        await hydration.hydrate((command,))
        next_state = await cache.get(before.automation_id)
        next_candle = await evaluator.decide(
            PositionWorkItem(command, False, state=next_state, snapshot_at=NOW), market("99.3")
        )
        return same_candle.decision, next_candle.decision

    same_candle, next_candle = asyncio.run(scenario())
    assert same_candle.kind is DecisionKind.WAIT
    assert same_candle.reason_code == "BUY_CANDLE_COOLDOWN"
    assert next_candle.kind is DecisionKind.BUY_MORE
