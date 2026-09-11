"""Dynamic thresholds through real Analytics HTTP, Worker and durable Core facts."""

import asyncio
from copy import deepcopy
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
from moex_sentinel.storage.models import TradeDecisionModel as CoreDecisionModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.analytics import MarketSourceSnapshot
from sentinel_contracts.broker_execution import BrokerConnection, BrokerOrderState, BrokerPosition
from tests.integration.test_analytics_worker_contract import ExecutionSession
from tests.market_analytics.test_app import NOW, Source, instrument
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.command_factory import command
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.composition import build_broker_runtime
from trading_automaton.config import StrategySettings
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, FactOutboxModel, TradeDecisionModel
from trading_automaton.storage.repository import LocalAutomationRepository


class Generations(Source):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.generation = 0

    def advance(self, *, swing="2", count=20, gap=False, bid="100", ask="101"):
        self.generation += 1
        self.clock[0] = NOW + timedelta(minutes=self.generation)
        item = instrument()
        item["market"]["order_book"]["captured_at"] = self.clock[0]
        item["market"]["order_book"]["bids"][0]["price"] = bid
        item["market"]["order_book"]["asks"][0]["price"] = ask
        item["candles"] = []
        for index in range(count):
            close = Decimal(100) + (Decimal(swing) if index % 2 else Decimal())
            item["candles"].append(
                {
                    "instrument_id": "instrument",
                    "open": "100",
                    "high": str(close + 2),
                    "low": "98",
                    "close": str(close),
                    "volume": 10,
                    "started_at": self.clock[0] - timedelta(minutes=count - index),
                    "is_complete": True,
                    "captured_at": self.clock[0],
                }
            )
        if gap:
            item["candles"][0]["started_at"] -= timedelta(minutes=1)
        self.instruments = [item]

    async def snapshot(self, request):
        self.calls.append(request)
        return MarketSourceSnapshot(
            snapshot_id=f"generation-{self.generation}",
            captured_at=self.clock[0],
            instruments=self.instruments,
        )


class FillingSession(ExecutionSession):
    """Synthetic broker retains actual fills across Worker restarts."""

    def __init__(self, repository, value, clock):
        super().__init__(repository, (value,))
        self.clock = clock
        self.quantity = Decimal()
        self.average_price = Decimal(100)

    async def get_positions(self, account_id):
        self.position_reads += 1
        return (BrokerPosition("instrument", self.quantity, self.average_price, Decimal(100), "RUB"),)

    async def dispatch_limit_order(self, request):
        latest = self.repository.get_latest_intent(str(self.commands[0].automation_id))
        assert latest is not None
        assert latest.idempotency_key == request.idempotency_key
        self.dispatched.append(request)
        if request.side == "BUY":
            self.quantity += request.quantity_lots
            self.average_price = request.limit_price
        else:
            assert request.side == "SELL"
            assert request.quantity_lots <= self.quantity
            self.quantity -= request.quantity_lots
        amount = request.limit_price * request.quantity_lots * request.lot_size
        return BrokerOrderState(
            broker_order_id=str(uuid4()),
            idempotency_key=request.idempotency_key,
            status="FILLED",
            requested_lots=request.quantity_lots,
            executed_lots=request.quantity_lots,
            requested_amount=amount,
            executed_amount=amount,
            estimated_commission=Decimal(1),
            executed_commission=Decimal(1),
            currency="RUB",
            executed_price=request.limit_price,
            executed_at=self.clock[0],
        )


class ContractHarness:
    def __init__(self, tmp_path, settings, *, pending_low):
        self.clock = [NOW]
        self.value = command()
        self.settings = settings
        self.worker_url = f"sqlite:///{tmp_path / 'dynamic-worker.db'}"
        self.engine = create_worker_engine(self.worker_url)
        Base.metadata.create_all(self.engine)
        self._repository()
        self.repository.cache_command(self.value)
        self.repository.save_cycle_state(
            TradingCycleState(str(self.value.automation_id), Decimal(pending_low), None, True, None, NOW)
        )
        self.source = Generations(self.clock)
        self.session = FillingSession(self.repository, self.value, self.clock)
        self.core_engine = create_engine("sqlite:///:memory:")
        CoreBase.metadata.create_all(self.core_engine)
        self.core_factory = sessionmaker(self.core_engine, expire_on_commit=False)
        with self.core_factory.begin() as db:
            db.add(user_broker_model(str(self.value.user_broker_id), self.value.account_id))
            db.add(instrument_model(str(self.value.instrument_id), str(self.value.user_broker_id)))
            db.add(
                TradingAutomationModel(
                    id=str(self.value.automation_id),
                    user_broker_id=str(self.value.user_broker_id),
                    instrument_id=str(self.value.instrument_id),
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        self.ingress = TradingFactIngressService(
            lambda: TradingFactsUnitOfWork(self.core_factory), TradingFactMapper(), now=lambda: self.clock[0]
        )
        self.bundle = None

    def _repository(self):
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.repository = LocalAutomationRepository(
            self.factory, fact_writer=FactOutboxWriter(clock=lambda: self.clock[0])
        )

    async def start(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(self.source, now=lambda: self.clock[0])),
            base_url="http://analytics",
        )
        self.bundle = await build_broker_runtime(
            BrokerConnection(str(self.value.broker_id), "TINVEST_SANDBOX", "fixture-target", "synthetic-token", True),
            self.repository,
            strategy_settings=self.settings,
            session_factory=lambda _: self.session,
            now=lambda: self.clock[0],
            analytics_client=AnalyticsClient(client),
        )
        await self.bundle.runtime.replace_commands((self.value,))

    async def tick(self, **generation):
        self.source.advance(**generation)
        await self.bundle.runtime.run_once()
        await self.bundle.tracking.wait_all()
        rows = self.repository.ready_fact_outbox(1000, now=self.clock[0] + timedelta(minutes=1), deadline_ms=0)
        result = self.ingress.publish([FactSynchronizationService._envelope(row) for row in rows])
        assert result.failures == ()
        with self.factory() as db:
            facts = db.scalars(
                select(FactOutboxModel)
                .where(FactOutboxModel.fact_kind == "TRADE_DECISION_RECORDED")
                .order_by(FactOutboxModel.sequence_number)
            ).all()
            assert len(facts) == 1
            payload = deepcopy(facts[-1].payload)
            decision = db.get(TradeDecisionModel, payload["decision_id"])
            assert decision.strategy_snapshot == payload["strategy_snapshot"]
            assert decision.reason_code == payload["reason_code"]
        with self.core_factory() as db:
            decision = db.get(CoreDecisionModel, payload["decision_id"])
            assert decision.indicators == payload["indicators"]
            assert decision.strategy_snapshot == payload["strategy_snapshot"]
        for accepted in result.results:
            self.repository.acknowledge_fact_outbox(
                str(accepted.automation_id),
                accepted_through_sequence=accepted.accepted_through_sequence,
                current_revision=accepted.current_revision,
            )
        return payload

    def persisted_payloads(self):
        result = {}
        for namespace, factory, model in (
            ("worker_decision", self.factory, TradeDecisionModel),
            ("core_decision", self.core_factory, CoreDecisionModel),
            ("core_order", self.core_factory, BrokerOrderModel),
        ):
            with factory() as db:
                for row in db.scalars(select(model)):
                    result[(namespace, row.id)] = {
                        column.key: deepcopy(getattr(row, column.key)) for column in model.__table__.columns
                    }
        return result

    async def restart(self):
        await self.bundle.close()
        self.engine.dispose()
        self.engine = create_worker_engine(self.worker_url)
        self._repository()
        self.session.repository = self.repository
        await self.start()

    async def close(self):
        if self.bundle is not None:
            await self.bundle.close()
        self.engine.dispose()
        self.core_engine.dispose()


@pytest.fixture
def strategy_settings(monkeypatch):
    for field in StrategySettings.model_fields.values():
        monkeypatch.delenv(field.validation_alias, raising=False)
    monkeypatch.setenv("STRATEGY_AVERAGING_STEP_PERCENT", "0.7")
    monkeypatch.setenv("STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT", "0.3")
    return StrategySettings()


def assert_thresholds(payload, averaging, net_profit, source):
    metrics = payload["indicators"]
    assert Decimal(metrics["averaging_step_percent"]) == Decimal(averaging)
    assert Decimal(metrics["minimum_net_profit_percent"]) == Decimal(net_profit)
    assert metrics["source"] == source


def test_new_volatility_changes_entry_and_keeps_persisted_decisions_and_orders_after_restart(
    tmp_path, strategy_settings
):
    """Catch sticky thresholds and historical payload rewriting on recalculation/restart."""

    async def scenario():
        harness = ContractHarness(tmp_path, strategy_settings, pending_low="99.5")
        try:
            await harness.start()
            high = await harness.tick(swing="2")
            assert_thresholds(high, "4", "2", "ADAPTIVE")
            assert high["reason_code"] == "ENTRY_REVERSAL_WAIT"
            assert harness.session.dispatched == []

            low = await harness.tick(swing="0.1")
            assert_thresholds(low, "0.2", "0.1", "ADAPTIVE")
            assert low["reason_code"] == "ENTRY_REVERSAL_CONFIRMED"
            assert low["decision"] == "BUY_MORE"
            assert low["requested_quantity_lots"] == strategy_settings.buy_order_lots
            assert low["best_bid"] == high["best_bid"]
            assert low["strategy_snapshot"] == high["strategy_snapshot"]
            assert low["strategy_snapshot"]["strategy_code"] == "ADAPTIVE_SCALPING"
            assert len(harness.session.dispatched) == 1
            original = harness.persisted_payloads()
            assert sum(namespace == "core_order" for namespace, _ in original) == 1
            with harness.core_factory() as db:
                order = db.scalars(select(BrokerOrderModel)).one()
                assert order.state == "FILLED"
                assert order.strategy_snapshot == low["strategy_snapshot"]
                original_order = (order.id, deepcopy(order.strategy_snapshot))

            await harness.restart()
            assert harness.persisted_payloads() == original
            newest = await harness.tick(swing="2")
            assert_thresholds(newest, "4", "2", "ADAPTIVE")
            assert newest["quantity_lots"] == strategy_settings.buy_order_lots
            assert newest["reason_code"] == "NO_THRESHOLD"
            assert len(harness.session.dispatched) == 1
            current = harness.persisted_payloads()
            assert {event_id: current[event_id] for event_id in original} == original
            with harness.core_factory() as db:
                order = db.get(BrokerOrderModel, original_order[0])
                assert order.strategy_snapshot == original_order[1]
                assert order.state == "FILLED"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_latest_net_profit_threshold_controls_partial_sale_at_unchanged_price(tmp_path, strategy_settings):
    """Catch a Worker that records fresh profit thresholds but sells using a stale one."""

    async def scenario():
        harness = ContractHarness(tmp_path, strategy_settings, pending_low="99.5")
        try:
            await harness.start()
            entry = await harness.tick(swing="0.1")
            assert entry["reason_code"] == "ENTRY_REVERSAL_CONFIRMED"
            high = await harness.tick(swing="2", bid="101.5", ask="101.6")
            assert_thresholds(high, "4", "2", "ADAPTIVE")
            assert high["reason_code"] == "NO_THRESHOLD"
            assert len(harness.session.dispatched) == 1

            low = await harness.tick(swing="0.1", bid="101.5", ask="101.6")
            assert_thresholds(low, "0.2", "0.1", "ADAPTIVE")
            assert low["best_bid"] == high["best_bid"]
            assert low["reason_code"] == "LIFO_LOT_TAKE_PROFIT"
            assert low["decision"] == "SELL_PART"
            assert len(harness.session.dispatched) == 2
            assert harness.session.dispatched[-1].side == "SELL"
            assert harness.session.quantity == 0
            with harness.core_factory() as db:
                orders = db.scalars(select(BrokerOrderModel)).all()
                assert len(orders) == 2
                assert {order.state for order in orders} == {"FILLED"}
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("count", "gap"), [(0, False), (14, False), (15, False), (19, False), (20, True)])
def test_history_loss_and_recovery_replace_metrics_without_sticky_sma_or_thresholds(
    tmp_path, strategy_settings, count, gap
):
    """Separate the 15-candle adaptation boundary from the continuous SMA20 buy gate."""

    async def scenario():
        harness = ContractHarness(tmp_path, strategy_settings, pending_low="100")
        try:
            await harness.start()
            initial = await harness.tick(swing="2")
            assert_thresholds(initial, "4", "2", "ADAPTIVE")
            assert initial["indicators"]["mean_20"] == "101"

            limited = await harness.tick(swing="0", count=count, gap=gap)
            if count < 15:
                assert_thresholds(limited, "0.7", "0.3", "STRATEGY")
            else:
                assert_thresholds(limited, "0.10", "0.05", "ADAPTIVE")
            assert limited["indicators"]["mean_20"] is None
            assert limited["indicators"]["mean_5"] == (None if count == 0 else "100")
            assert limited["reason_code"] == "BUY_WINDOW_UNCONFIRMED"

            restored = await harness.tick(swing="0.1")
            assert_thresholds(restored, "0.2", "0.1", "ADAPTIVE")
            assert restored["indicators"]["mean_20"] == "100.05"
            assert restored["reason_code"] == "ENTRY_REVERSAL_WAIT"
            assert harness.session.dispatched == []
            assert len(harness.source.calls) == 3
        finally:
            await harness.close()

    asyncio.run(scenario())
