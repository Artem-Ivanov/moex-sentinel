"""Real Analytics HTTP, Worker decisions and durable facts without a live broker."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from market_analytics.app import create_app
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.models import Base as CoreBase
from moex_sentinel.storage.models import BrokerOrderModel, TradingAutomationModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import BrokerConnection, BrokerOrderState, BrokerPosition
from tests.market_analytics.market_source_helpers import NOW, Source, instrument
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.command_factory import command
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.composition import build_broker_runtime
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import CommissionQuote
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, TradeDecisionModel
from trading_automaton.storage.repository import LocalAutomationRepository


class ObservedRepository(LocalAutomationRepository):
    def __init__(self, factory, clock, *, expire_after_commit=False):
        super().__init__(factory, fact_writer=FactOutboxWriter(clock=lambda: clock[0]))
        self.clock = clock
        self.expire_after_commit = expire_after_commit
        self.committed = []

    def save_decision_batch(self, items, *, occurred_at):
        result = super().save_decision_batch(items, occurred_at=occurred_at)
        self.committed.append(items)
        if self.expire_after_commit:
            self.clock[0] += timedelta(seconds=3)
        return result


class ExecutionSession:
    """The only external boundary; every market method deliberately fails."""

    def __init__(self, repository, commands):
        self.repository = repository
        self.commands = commands
        self.dispatched = []
        self.position_reads = 0
        self.closed = False

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    def create_market_data_stream(self):
        raise AssertionError("Worker attempted a direct market stream")

    async def get_candles(self, *args):
        raise AssertionError("Worker attempted direct historical market access")

    async def get_order_book(self, *args):
        raise AssertionError("Worker attempted direct order book access")

    async def get_positions(self, account_id):
        self.position_reads += 1
        return tuple(
            BrokerPosition(item.external_instrument_id, Decimal(), Decimal(), Decimal(100), "RUB")
            for item in self.commands
            if item.account_id == account_id
        )

    async def get_free_cash(self, account_id, currency):
        return Decimal(10000)

    async def quote(self, request, side):
        return CommissionQuote(Decimal(1010), Decimal(1))

    async def dispatch_limit_order(self, request):
        # A new DB read proves the whole batch was committed before SDK entry.
        assert len(self.repository.committed) == 1
        assert all(self.repository.get_latest_intent(str(item.automation_id)) is not None for item in self.commands)
        self.dispatched.append(request)
        return BrokerOrderState(
            broker_order_id=str(uuid4()),
            idempotency_key=request.idempotency_key,
            status="FILLED",
            requested_lots=request.quantity_lots,
            executed_lots=request.quantity_lots,
            requested_amount=request.limit_price * request.quantity_lots * request.lot_size,
            executed_amount=request.limit_price * request.quantity_lots * request.lot_size,
            estimated_commission=Decimal(1),
            executed_commission=Decimal(1),
            currency="RUB",
            executed_price=request.limit_price,
            executed_at=NOW,
        )


def assert_core_accepts_facts(repository, commands, expected_state):
    engine = create_engine("sqlite:///:memory:")
    CoreBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        for value in commands:
            session.add(user_broker_model(str(value.user_broker_id), value.account_id))
            session.add(instrument_model(str(value.instrument_id), str(value.user_broker_id)))
            session.add(
                TradingAutomationModel(
                    id=str(value.automation_id),
                    user_broker_id=str(value.user_broker_id),
                    instrument_id=str(value.instrument_id),
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    rows = repository.ready_fact_outbox(1000, now=NOW + timedelta(minutes=1), deadline_ms=0)
    facts = [FactSynchronizationService._envelope(row) for row in rows]
    ingress = TradingFactIngressService(lambda: TradingFactsUnitOfWork(factory), TradingFactMapper(), now=lambda: NOW)
    result = ingress.publish(facts)
    assert result.failures == ()
    with factory() as session:
        orders = session.scalars(select(BrokerOrderModel)).all()
        assert len(orders) == (len(commands) if expected_state is not None else 0)
        assert {order.state for order in orders} == ({expected_state} if expected_state is not None else set())
    engine.dispose()
    return result


@pytest.mark.parametrize("expire_after_commit", [False, True])
def test_two_positions_complete_one_http_batch_and_publish_terminal_facts(tmp_path, expire_after_commit):
    async def scenario():
        clock = [NOW]
        values = tuple(
            command(automation=f"auto-{i}", account=f"account-{i}", instrument=f"market-{i}") for i in range(2)
        )
        engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        repository = ObservedRepository(factory, clock, expire_after_commit=expire_after_commit)
        for value in values:
            repository.cache_command(value)
            repository.save_cycle_state(TradingCycleState(str(value.automation_id), Decimal(90), None, True, None, NOW))
        source = Source([instrument(value.external_instrument_id) for value in values])
        session = ExecutionSession(repository, values)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(source, now=lambda: clock[0])), base_url="http://analytics"
        )
        bundle = await build_broker_runtime(
            BrokerConnection(str(values[0].broker_id), "TINVEST_SANDBOX", "fixture-target", "synthetic-token", True),
            repository,
            strategy_settings=StrategySettings(),
            session_factory=lambda _: session,
            now=lambda: clock[0],
            analytics_client=AnalyticsClient(client),
        )
        try:
            await bundle.runtime.replace_commands(values)
            await bundle.runtime.run_once()
            await bundle.tracking.wait_all()
            assert len(source.calls) == 1
            assert len(repository.committed) == 1
            assert len(repository.committed[0]) == 2
            assert {item.iteration_id for item in repository.committed[0]} == {"generation-1"}
            assert {item.indicators["mean_20"] for item in repository.committed[0]} == {"110.5"}
            assert {item.reason_code for item in repository.committed[0]} == {"ENTRY_REVERSAL_CONFIRMED"}
            expected = "CANCELLED" if expire_after_commit else "FILLED"
            assert len(session.dispatched) == (0 if expire_after_commit else 2)
            for value in values:
                latest = repository.get_latest_intent(str(value.automation_id))
                assert latest is not None
                assert latest.state == expected
                assert latest.terminal_at is not None
                assert repository.get_active_intent(str(value.automation_id)) is None
                assert len(repository.list_open_lots(str(value.automation_id))) == (0 if expire_after_commit else 1)
                assert await bundle.runtime._iteration._tick._cash.reserved(value.account_id, value.currency) == 0
            assert_core_accepts_facts(repository, values, expected)
            await bundle.runtime.run_once()
            assert len(repository.committed) == 1
        finally:
            await bundle.close()
            engine.dispose()
        assert client.is_closed
        assert session.closed

    asyncio.run(scenario())


def test_real_http_outage_runs_recovery_without_market_fallback_then_recovers(tmp_path):
    async def scenario():
        clock = [NOW]
        value = command()
        engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        repository = ObservedRepository(factory, clock)
        repository.cache_command(value)
        repository.save_cycle_state(TradingCycleState(str(value.automation_id), Decimal(90), None, True, None, NOW))
        source = Source(error=httpx.ReadTimeout("synthetic outage"))
        session = ExecutionSession(repository, (value,))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(source, now=lambda: clock[0])), base_url="http://analytics"
        )
        bundle = await build_broker_runtime(
            BrokerConnection(str(value.broker_id), "TINVEST_SANDBOX", "fixture-target", "synthetic-token", True),
            repository,
            strategy_settings=StrategySettings(),
            session_factory=lambda _: session,
            now=lambda: clock[0],
            analytics_client=AnalyticsClient(client),
        )
        try:
            await bundle.runtime.replace_commands((value,))
            await bundle.runtime.run_once()
            assert session.position_reads == 1
            assert repository.committed == session.dispatched == []
            with factory() as db:
                assert db.scalars(select(TradeDecisionModel)).all() == []
            # Coordinator publishes the recovery facts before allowing another
            # hydration; exercise that existing durability gate here as well.
            accepted = assert_core_accepts_facts(repository, (value,), None)
            for result in accepted.results:
                repository.acknowledge_fact_outbox(
                    str(result.automation_id),
                    accepted_through_sequence=result.accepted_through_sequence,
                    current_revision=result.current_revision,
                )
            source.error = None
            await bundle.runtime.run_once()
            await bundle.tracking.wait_all()
            assert session.position_reads == 2
            assert len(repository.committed) == len(session.dispatched) == 1
        finally:
            await bundle.close()
            engine.dispose()

    asyncio.run(scenario())
