import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.business_audit import BusinessAuditStage
from tests.trading_automaton.command_factory import command
from tests.trading_automaton.services.test_position_state_hydration_service import PreparedMetrics
from trading_automaton.domain.dtos import PositionConsistencyResult
from trading_automaton.domain.storage_dtos import TradeLotRecord
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.services.position_state_hydration import PositionStateCacheService, PositionStateHydrationService
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, BusinessAuditEventModel, CachedAutomationModel, FactOutboxModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 10, 2, tzinfo=UTC)


class Portfolio:
    quantity = Decimal(2)
    average_price = Decimal(100)

    async def position(self, account_id, instrument_id):
        return BrokerPosition(instrument_id, self.quantity, self.average_price, Decimal(101), "RUB")


class Consistency:
    def __init__(self):
        self.calls = 0
        self.failed = False
        self.error = False

    def reconcile(self, *, automation_id, broker_lots, average_price):
        self.calls += 1
        if self.error:
            raise RuntimeError("unexpected reconciliation failure")
        if self.failed:
            return PositionConsistencyResult(False, (), "POSITION_RECONCILIATION_REQUIRED")
        return PositionConsistencyResult(
            True,
            (
                TradeLotRecord(
                    "lot", automation_id, None, "RECONCILED", broker_lots, broker_lots, average_price, Decimal(), NOW
                ),
            ),
        )


class Harness:
    def __init__(self, tmp_path, *, cycle_known=True, writer=None):
        engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.repository = LocalAutomationRepository(self.factory, fact_writer=writer)
        self.command = command()
        self.repository.cache_command(self.command)
        self.cycle_id = str(uuid4()) if cycle_known else None
        self.set_cycle(self.cycle_id)
        self.portfolio = Portfolio()
        self.consistency = Consistency()
        self.cache = PositionStateCacheService()
        self.restart()

    def set_cycle(self, cycle_id):
        with self.factory.begin() as db:
            db.get(CachedAutomationModel, str(self.command.automation_id)).position_cycle_id = cycle_id

    def restart(self):
        self.service = PositionStateHydrationService(
            self.repository,
            self.portfolio,
            self.cache,
            consistency=self.consistency,
            audit=BusinessAuditService(self.repository, now=lambda: NOW),
            now=lambda: NOW,
            prepared_metrics=PreparedMetrics(),
        )

    async def hydrate(self):
        await self.service.hydrate((self.command,))

    def counts(self):
        with self.factory() as db:
            return (
                db.scalar(select(func.count()).select_from(BusinessAuditEventModel)),
                db.scalar(select(func.count()).select_from(FactOutboxModel)),
                self.repository.get_state(str(self.command.automation_id)).last_sequence_number,
            )

    def ack(self):
        state = self.repository.get_state(str(self.command.automation_id))
        self.repository.acknowledge_fact_outbox(
            state.automation_id, accepted_through_sequence=state.last_sequence_number, current_revision=state.revision
        )


def test_unchanged_success_reconciles_each_pass_without_new_audit_or_sequence(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        assert harness.counts() == (2, 2, 2)
        harness.ack()
        before = harness.counts()
        await harness.hydrate()
        assert harness.counts() == before
        await harness.hydrate()
        assert harness.consistency.calls == 3
        assert harness.counts() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["quantity", "price", "cycle", "account", "broker", "instrument", "restart"])
def test_changed_scope_result_or_restart_records_success_again(tmp_path, change):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        harness.ack()
        if change == "quantity":
            harness.portfolio.quantity = Decimal(3)
        elif change == "price":
            harness.portfolio.average_price = Decimal(102)
        elif change == "cycle":
            harness.set_cycle(str(uuid4()))
        elif change == "restart":
            harness.restart()
        else:
            field = {"account": "account_id", "broker": "broker_id", "instrument": "external_instrument_id"}[change]
            harness.command = harness.command.model_copy(update={field: uuid4() if change == "broker" else "changed"})
        await harness.hydrate()
        assert harness.counts() == (4, 2, 4)

    asyncio.run(scenario())


def test_missing_cycle_identity_never_suppresses_success(tmp_path):
    async def scenario():
        harness = Harness(tmp_path, cycle_known=False)
        await harness.hydrate()
        harness.ack()
        await harness.hydrate()
        assert harness.counts() == (4, 2, 4)

    asyncio.run(scenario())


def test_failures_always_record_and_recovery_records_previous_success_again(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        harness.ack()
        harness.consistency.failed = True
        await harness.hydrate()
        harness.ack()
        await harness.hydrate()
        assert await harness.cache.get(str(harness.command.automation_id)) is None
        harness.ack()
        harness.consistency.failed = False
        await harness.hydrate()
        assert harness.counts() == (8, 2, 8)

    asyncio.run(scenario())


def test_unexpected_error_records_failed_pair_and_re_raises(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        harness.ack()
        harness.consistency.error = True
        with pytest.raises(RuntimeError, match="unexpected reconciliation"):
            await harness.hydrate()
        with harness.factory() as db:
            stages = list(db.scalars(select(BusinessAuditEventModel.stage)))
        assert stages.count(BusinessAuditStage.POSITION_RECONCILIATION_FAILED.value) == 1
        assert harness.counts() == (4, 2, 4)
        harness.ack()
        harness.consistency.error = False
        await harness.hydrate()
        assert harness.counts() == (6, 2, 6)

    asyncio.run(scenario())


def test_pending_outbox_is_untouched_and_does_not_advance_success_memo(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        before = harness.counts()
        harness.portfolio.quantity = Decimal(3)
        await harness.hydrate()
        assert harness.counts() == before
        assert harness.consistency.calls == 1
        harness.ack()
        await harness.hydrate()
        assert harness.counts() == (4, 2, 4)

    asyncio.run(scenario())


def test_pair_failure_rolls_back_audit_outbox_sequence_and_allows_retry(tmp_path):
    class FailingWriter(FactOutboxWriter):
        fail = True

        def append(self, *args, **kwargs):
            if self.fail and kwargs["payload"].stage == BusinessAuditStage.POSITION_RECONCILED.value:
                raise RuntimeError("audit append failed")
            return super().append(*args, **kwargs)

    async def scenario():
        writer = FailingWriter()
        harness = Harness(tmp_path, writer=writer)
        with pytest.raises(RuntimeError, match="audit append failed"):
            await harness.hydrate()
        assert harness.counts() == (0, 0, 0)
        writer.fail = False
        await harness.hydrate()
        assert harness.counts() == (2, 2, 2)

    asyncio.run(scenario())


def test_removed_automation_is_pruned_and_reappearance_records_success(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        await harness.hydrate()
        harness.ack()
        await harness.service.hydrate(())
        await harness.hydrate()
        assert harness.counts() == (4, 2, 4)

    asyncio.run(scenario())
