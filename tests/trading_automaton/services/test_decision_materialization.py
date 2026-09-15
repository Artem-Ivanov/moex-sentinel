import asyncio
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamOrderBook
from sentinel_contracts.trading import DecisionKind
from tests.trading_automaton.services.test_position_state_hydration_service import (
    NOW,
    Candles,
    Portfolio,
    Repository,
    command,
)
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import (
    PositionConsistencyResult,
    PositionEvaluationResult,
    PositionWorkItem,
    PreparedDecision,
    TradeDecision,
)
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.services.decision_materialization import DecisionMaterializerService
from trading_automaton.services.position_state_hydration import PositionStateCacheService, PositionStateHydrationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

HYDRATION_ID = "00000000-0000-4000-8000-000000000601"
FALLBACK_ID = "00000000-0000-4000-8000-000000000602"
INTENT_ID = "00000000-0000-4000-8000-000000000603"


class ConsistentPosition:
    def reconcile(self, **values):
        return PositionConsistencyResult(True, ())


@pytest.mark.parametrize("kind", [DecisionKind.BUY_MORE, DecisionKind.SELL_PART, DecisionKind.WAIT])
def test_hydration_decision_dispatch_and_durable_audit_share_process(tmp_path, kind):
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'audit.sqlite'}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(
        sessionmaker(engine, expire_on_commit=False), fact_writer=FactOutboxWriter(clock=lambda: NOW)
    )
    repository.cache_command(command())
    audit = BusinessAuditService(repository, now=lambda: NOW)

    async def scenario():
        cache = PositionStateCacheService()
        hydration = PositionStateHydrationService(
            Repository(),
            Portfolio(),
            Candles(),
            cache,
            consistency=ConsistentPosition(),
            audit=audit,
            now=lambda: NOW,
            id_factory=lambda: HYDRATION_ID,
        )
        await hydration.hydrate((command(),))
        state = await cache.get(str(command().automation_id))
        assert state is not None
        result = await materialize(state, kind)
        assert result is not None
        audit.record_decision_process(**result.audit_values)
        return result

    try:
        result = asyncio.run(scenario())
        facts = repository.ready_fact_outbox(100, now=NOW, deadline_ms=0)
        assert result.item.process_id == HYDRATION_ID
        assert result.audit_values["process_id"] == HYDRATION_ID
        assert result.item.decision == kind.value
        assert result.item.reason_code == "TEST_DECISION"
        assert result.item.indicators["last_candle_at"] == NOW.isoformat()
        if kind is DecisionKind.WAIT:
            assert result.request is None
            assert result.item.intent is None
        else:
            assert result.request.process_id == HYDRATION_ID
            assert result.request.idempotency_key == INTENT_ID
            assert result.request.quantity_lots == 1
            assert result.request.limit_price == Decimal("101")
        assert {fact.payload["process_id"] for fact in facts} == {HYDRATION_ID}
        stages = [fact.payload["stage"] for fact in facts]
        assert stages[:2] == [
            BusinessAuditStage.POSITION_RECONCILIATION_STARTED.value,
            BusinessAuditStage.POSITION_RECONCILED.value,
        ]
        assert BusinessAuditStage.MARKET_DATA_RECEIVED.value in stages
        assert BusinessAuditStage.STRATEGY_DECISION_MADE.value in stages
        assert all(datetime.fromisoformat(fact.payload["occurred_at"]) == NOW for fact in facts)
    finally:
        engine.dispose()


async def materialize(
    state,
    kind=DecisionKind.BUY_MORE,
    *,
    cash=None,
    pending_cash=None,
    id_factory=lambda: FALLBACK_ID,
    intent_id=INTENT_ID,
):
    actionable = kind is not DecisionKind.WAIT
    prepared = PreparedDecision(
        command(),
        "snapshot",
        NOW,
        PositionEvaluationResult(
            TradeDecision(kind, 1 if actionable else 0, Decimal("101") if actionable else None, "TEST_DECISION"),
            state,
            Decimal("0.5") if actionable else Decimal(),
        ),
    )
    market = InstrumentMarketState(
        "instrument",
        StreamOrderBook(
            "instrument", (OrderBookLevel(Decimal("100"), 10),), (OrderBookLevel(Decimal("101"), 10),), NOW, True
        ),
    )
    return await DecisionMaterializerService(id_factory=id_factory, settings=StrategySettings()).materialize(
        prepared,
        PositionWorkItem(command(), False),
        market,
        None,
        cash=cash,
        pending_cash={} if pending_cash is None else pending_cash,
        snapshot_at=NOW,
        intent_id=intent_id,
    )


@pytest.mark.parametrize("process_id", [None, "", "invalid-uuid"])
def test_missing_or_invalid_hydration_process_uses_factory_fallback(process_id):
    async def scenario():
        cache = PositionStateCacheService()
        hydration = PositionStateHydrationService(Repository(), Portfolio(), Candles(), cache, now=lambda: NOW)
        await hydration.hydrate((command(),))
        state = await cache.get(str(command().automation_id))
        return await materialize(state.model_copy(update={"process_id": process_id}))

    result = asyncio.run(scenario())
    assert result.item.process_id == FALLBACK_ID
    assert result.request.process_id == FALLBACK_ID
    assert result.audit_values["process_id"] == FALLBACK_ID
    assert UUID(result.item.process_id)


def test_active_intent_wait_does_not_publish_pre_execution_cycle():
    async def scenario():
        cache = PositionStateCacheService()
        hydration = PositionStateHydrationService(Repository(), Portfolio(), Candles(), cache, now=lambda: NOW)
        await hydration.hydrate((command(),))
        state = await cache.get(str(command().automation_id))
        assert state.has_active_intent
        return await materialize(state, DecisionKind.WAIT)

    assert asyncio.run(scenario()).item.cycle_state is None


def test_intent_id_failure_does_not_reserve_pending_cash():
    class Cash:
        async def available(self, account_id, currency):
            return Decimal("100000")

        async def reserved(self, account_id, currency):
            return Decimal()

    failure = RuntimeError("ID generation failed")
    pending_cash = {(command().account_id, command().currency.upper()): Decimal("10")}
    before = pending_cash.copy()

    def fail_id():
        raise failure

    async def scenario():
        cache = PositionStateCacheService()
        hydration = PositionStateHydrationService(Repository(), Portfolio(), Candles(), cache, now=lambda: NOW)
        await hydration.hydrate((command(),))
        state = await cache.get(str(command().automation_id))
        await materialize(
            state.model_copy(update={"process_id": HYDRATION_ID}),
            cash=Cash(),
            pending_cash=pending_cash,
            id_factory=fail_id,
            intent_id=None,
        )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(scenario())
    assert caught.value is failure
    assert pending_cash == before
