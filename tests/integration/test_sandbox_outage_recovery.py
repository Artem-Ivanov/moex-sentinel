"""Sandbox outage behavior through Analytics HTTP, Worker SQLite and Core ingress."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from moex_sentinel.storage.models import BrokerOrderModel, PositionLotModel, TradeExecutionModel, TradingAutomationModel
from moex_sentinel.storage.models import TradeDecisionModel as CoreDecisionModel
from sentinel_contracts.analytics import MarketSourceSnapshot
from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition
from sentinel_contracts.trading import AutomationState
from tests.integration.test_analytics_worker_contract import ExecutionSession
from tests.integration.test_dynamic_strategy_contract import ContractHarness
from tests.market_analytics.test_app import NOW, instrument
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model
from tests.trading_automaton.command_factory import command
from trading_automaton.config import StrategySettings
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel, TradeDecisionModel


class RecoveringSource:
    """Controlled upstream timestamps; advancing wall time never refreshes a book."""

    def __init__(self, clock, values):
        self.clock = clock
        self.values = values
        self.generation = 0
        self.error = None
        self.items = []

    def publish(self, *, stale=(), unavailable=False):
        self.generation += 1
        self.items = []
        if unavailable:
            return
        for value in self.values:
            item = instrument(value.external_instrument_id)
            shift = self.clock[0] - NOW
            item["market"]["order_book"]["captured_at"] += shift
            if value.external_instrument_id in stale:
                item["market"]["order_book"]["captured_at"] -= timedelta(milliseconds=2001)
            for candle in item["candles"]:
                candle["started_at"] += self.clock[0].replace(second=0, microsecond=0) - NOW
                candle["captured_at"] += shift
            self.items.append(item)

    async def snapshot(self, request):
        if self.error is not None:
            raise self.error
        return MarketSourceSnapshot(
            snapshot_id=f"outage-generation-{self.generation}",
            captured_at=self.clock[0],
            instruments=self.items,
        )


class PersistentFills(ExecutionSession):
    """External synthetic broker retains its fills independently of Worker restarts."""

    def __init__(self, repository, values, clock):
        super().__init__(repository, values)
        self.clock = clock
        self.quantities = {item.external_instrument_id: Decimal() for item in values}

    async def get_positions(self, account_id):
        self.position_reads += 1
        return tuple(
            BrokerPosition(
                item.external_instrument_id,
                self.quantities[item.external_instrument_id],
                Decimal(101),
                Decimal(100),
                "RUB",
            )
            for item in self.commands
            if item.account_id == account_id
        )

    async def dispatch_limit_order(self, request):
        # Record SDK entry before validation: production contains broker errors,
        # so an AssertionError alone cannot prove that a second call never happened.
        self.dispatched.append(request)
        value = next(item for item in self.commands if item.external_instrument_id == request.instrument_id)
        intent = self.repository.get_latest_intent(str(value.automation_id))
        assert intent is not None
        assert intent.idempotency_key == request.idempotency_key
        assert request.idempotency_key not in {item.idempotency_key for item in self.dispatched[:-1]}
        assert request.side == "BUY"
        self.quantities[request.instrument_id] += request.quantity_lots
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


class OutageHarness(ContractHarness):
    def __init__(self, tmp_path, *, held=False):
        super().__init__(tmp_path, StrategySettings(), pending_low="90")
        if held:
            self.value = self.value.model_copy(update={"state": AutomationState.HOLD})
            with self.factory.begin() as db:
                db.get(CachedAutomationModel, str(self.value.automation_id)).state = "HOLD"
            with self.core_factory.begin() as db:
                db.get(TradingAutomationModel, str(self.value.automation_id)).state = "HOLD"
        peer = command(automation="peer", account="peer-account", instrument="peer")
        self.values = (self.value, peer)
        self.repository.cache_command(peer)
        self.repository.save_cycle_state(TradingCycleState(str(peer.automation_id), Decimal(90), None, True, None, NOW))
        with self.core_factory.begin() as db:
            db.add(user_broker_model(str(peer.user_broker_id), peer.account_id))
            db.add(instrument_model(str(peer.instrument_id), str(peer.user_broker_id)))
            db.add(
                TradingAutomationModel(
                    id=str(peer.automation_id),
                    user_broker_id=str(peer.user_broker_id),
                    instrument_id=str(peer.instrument_id),
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        self.source = RecoveringSource(self.clock, self.values)
        self.session = PersistentFills(self.repository, self.values, self.clock)

    async def start(self):
        await super().start()
        await self.bundle.runtime.replace_commands(self.values)

    async def run(self):
        await self.bundle.runtime.run_once()
        await self.bundle.tracking.wait_all()

    def pending(self):
        rows = self.repository.ready_fact_outbox(1000, now=self.clock[0] + timedelta(minutes=1), deadline_ms=0)
        return [FactSynchronizationService._envelope(row) for row in rows]

    def deliver(self, *, acknowledge=True):
        facts = self.pending()
        result = self.ingress.publish(facts)
        assert not result.failures
        if acknowledge:
            for accepted in result.results:
                self.repository.acknowledge_fact_outbox(
                    str(accepted.automation_id),
                    accepted_through_sequence=accepted.accepted_through_sequence,
                    current_revision=accepted.current_revision,
                )
        return facts

    def decisions(self):
        with self.factory() as db:
            return [
                (row.automation_id, row.decision, row.occurred_at) for row in db.scalars(select(TradeDecisionModel))
            ]


@pytest.fixture(autouse=True)
def isolated_strategy_settings(monkeypatch):
    for field in StrategySettings.model_fields.values():
        monkeypatch.delenv(field.validation_alias, raising=False)


def test_stale_instrument_does_not_block_fresh_peer_and_recovers_only_with_new_book(tmp_path):
    """Catch cross-instrument blocking and treating a fresh envelope as a fresh book."""

    async def scenario():
        harness = OutageHarness(tmp_path)
        try:
            await harness.start()
            harness.source.publish(stale=("instrument",))
            await harness.run()
            assert [item.instrument_id for item in harness.session.dispatched] == ["peer"]
            assert harness.repository.get_latest_intent(str(harness.value.automation_id)) is None
            harness.deliver()

            # Source is responsive, but it keeps returning the same old books.
            harness.clock[0] += timedelta(seconds=30)
            await harness.run()
            assert len(harness.session.dispatched) == 1
            assert len(harness.decisions()) == 1
            harness.deliver()

            harness.source.publish()
            await harness.run()
            harness.deliver()
            assert [item.instrument_id for item in harness.session.dispatched] == ["peer", "instrument"]
            with harness.core_factory() as db:
                assert {row.instrument_id for row in db.scalars(select(BrokerOrderModel))} == {
                    str(value.instrument_id) for value in harness.values
                }
            decisions = [item for item in harness.decisions() if item[0] == str(harness.value.automation_id)]
            assert len(decisions) == 1
            assert decisions[0][2] - NOW == timedelta(seconds=30)
        finally:
            await harness.close()

    asyncio.run(scenario())


class AcceptedWithoutResponse(PersistentFills):
    """Broker executes once, loses its response, then temporarily denies reconciliation."""

    def __init__(self, repository, values, clock):
        super().__init__(repository, values, clock)
        self.unavailable = True
        self.accepted = None

    def require_available(self):
        if self.unavailable:
            raise TInvestAdapterError("BROKER_UNAVAILABLE", "Synthetic broker unavailable", retryable=True)

    async def dispatch_limit_order(self, request):
        self.accepted = await super().dispatch_limit_order(request)
        raise TimeoutError("Synthetic lost order response after acceptance")

    async def get_order_state(self, account_id, broker_order_id):
        self.require_available()
        assert self.accepted is not None
        assert broker_order_id == self.accepted.broker_order_id
        return self.accepted

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        self.require_available()
        assert self.accepted is not None
        assert idempotency_key == self.accepted.idempotency_key
        return self.accepted

    async def inspect_position(self, account_id, instrument_id):
        self.require_available()
        return {"quantity_lots": str(self.quantities[instrument_id]), "currency": "RUB"}

    async def inspect_recent_operations(self, account_id, instrument_id, limit):
        self.require_available()
        return ()


def test_accepted_order_with_lost_response_reconciles_after_outage_restart_without_second_dispatch(tmp_path):
    """Catch treating an uncertain outcome as permission to resend or apply its fill twice."""

    async def scenario():
        harness = OutageHarness(tmp_path, held=True)
        harness.session = AcceptedWithoutResponse(harness.repository, harness.values, harness.clock)
        peer = harness.values[1]
        automation_id = str(peer.automation_id)
        try:
            await harness.start()
            harness.source.publish()
            await harness.run()
            uncertain = harness.repository.get_active_intent(automation_id)
            assert uncertain is not None
            assert uncertain.state == "UNCERTAIN"
            assert uncertain.broker_order_id is None
            assert len(harness.session.dispatched) == 1
            assert harness.repository.list_open_lots(automation_id) == []
            harness.deliver()

            # Even fresh quotes cannot authorize resubmission of an uncertain intent.
            for _ in range(2):
                harness.clock[0] += timedelta(seconds=5)
                harness.source.publish()
                with pytest.raises(TInvestAdapterError) as raised:
                    await harness.run()
                assert raised.value.code == "BROKER_UNAVAILABLE"
                assert raised.value.retryable
                assert harness.repository.get_active_intent(automation_id).state == "UNCERTAIN"
                assert harness.repository.list_open_lots(automation_id) == []
                assert len(harness.session.dispatched) == 1
                harness.deliver()

            await harness.restart()
            with pytest.raises(TInvestAdapterError):
                await harness.run()
            assert harness.repository.get_active_intent(automation_id).idempotency_key == uncertain.idempotency_key
            assert len(harness.session.dispatched) == 1
            harness.deliver()

            # Recovery does not require a fresh market: the existing order must be reconciled first.
            harness.clock[0] += timedelta(seconds=50)
            harness.source.publish(unavailable=True)
            harness.session.unavailable = False
            await harness.run()
            assert harness.repository.get_active_intent(automation_id) is None
            final = harness.repository.get_latest_intent(automation_id)
            assert final.state == "FILLED"
            assert final.idempotency_key == uncertain.idempotency_key
            lots = harness.repository.list_open_lots(automation_id)
            assert len(lots) == 1
            assert lots[0].remaining_lots == uncertain.quantity_lots
            assert len(harness.session.dispatched) == 1
            harness.deliver(acknowledge=False)

            await harness.restart()
            harness.deliver()
            await harness.run()
            harness.deliver()
            harness.source.publish()
            await harness.run()
            harness.deliver()
            assert len(harness.session.dispatched) == 1
            assert len(harness.repository.list_open_lots(automation_id)) == 1
            with harness.core_factory() as db:
                orders = db.scalars(select(BrokerOrderModel)).all()
                assert len(orders) == 1
                assert orders[0].state == "FILLED"
                assert len(db.scalars(select(TradeExecutionModel)).all()) == 1
                assert len(db.scalars(select(PositionLotModel)).all()) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
def test_outage_restart_preserves_pending_facts_and_replays_lost_ack_without_duplicate_orders(tmp_path, failure):
    """Catch lost SQLite recovery facts, premature trading and duplicate delivery after restart."""

    async def scenario():
        harness = OutageHarness(tmp_path)
        try:
            await harness.start()
            harness.source.publish(unavailable=True)
            if failure == "timeout":
                harness.source.error = httpx.ReadTimeout("synthetic source timeout")
            await harness.run()
            assert harness.decisions() == []
            assert harness.session.dispatched == []
            initial = harness.pending()
            assert initial  # Recovery cycles are durable even without market data.
            original = [item.model_dump(mode="json") for item in initial]

            harness.clock[0] += timedelta(seconds=60)
            await harness.restart()
            assert [item.model_dump(mode="json") for item in harness.pending()] == original
            await harness.run()
            assert harness.decisions() == []
            assert harness.session.dispatched == []
            assert harness.session.position_reads >= 4
            harness.deliver()

            harness.source.error = None
            harness.source.publish()
            await harness.run()
            assert len(harness.session.dispatched) == 2
            assert {item[2] - NOW for item in harness.decisions()} == {timedelta(seconds=60)}
            pending_fills = harness.deliver(acknowledge=False)
            assert pending_fills
            before = harness.persisted_payloads()

            # Core committed the batch, but Worker crashed before receiving ACK.
            await harness.restart()
            assert [item.model_dump(mode="json") for item in harness.pending()] == [
                item.model_dump(mode="json") for item in pending_fills
            ]
            harness.deliver()
            assert harness.persisted_payloads() == before
            with harness.factory() as db:
                assert db.scalars(select(FactOutboxModel)).all() == []
            with harness.core_factory() as db:
                assert len(db.scalars(select(BrokerOrderModel)).all()) == 2
                assert len(db.scalars(select(CoreDecisionModel)).all()) == 2
            await harness.run()
            assert len(harness.session.dispatched) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_explicit_hold_survives_outage_restart_and_fresh_peer_resumption(tmp_path):
    """Catch recovery implicitly resuming an automation that the user put on HOLD."""

    async def scenario():
        harness = OutageHarness(tmp_path, held=True)
        try:
            await harness.start()
            harness.source.publish(unavailable=True)
            await harness.run()
            assert harness.session.dispatched == []
            await harness.restart()
            harness.deliver()
            harness.clock[0] += timedelta(seconds=60)
            harness.source.publish()
            await harness.run()
            harness.deliver()
            assert [item.instrument_id for item in harness.session.dispatched] == ["peer"]
            assert harness.repository.get_state(str(harness.value.automation_id)).state == "HOLD"
            assert harness.repository.get_latest_intent(str(harness.value.automation_id)) is None
            with harness.core_factory() as db:
                assert db.get(TradingAutomationModel, str(harness.value.automation_id)).state == "HOLD"
            assert all(item[0] != str(harness.value.automation_id) for item in harness.decisions())
        finally:
            await harness.close()

    asyncio.run(scenario())
