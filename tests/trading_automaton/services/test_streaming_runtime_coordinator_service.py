import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand, BrokerPositionBootstrap
from tests.trading_automaton.command_factory import command as baseline_command
from tests.trading_automaton.command_factory import decision_item
from trading_automaton.config import StrategySettings
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.services.streaming_runtime_coordinator import LOGGER, StreamingRuntimeCoordinatorService
from trading_automaton.storage.models import Base, LocalIntentModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command(state=AutomationState.IN_QUEUE) -> AutomationCommand:
    return baseline_command(state=state)


def bootstrap_command() -> AutomationCommand:
    return baseline_command(
        state=AutomationState.HOLD,
        bootstrap=BrokerPositionBootstrap(
            position_cycle_id="00000000-0000-4000-8000-000000000501",
            position_lot_id="00000000-0000-4000-8000-000000000502",
            quantity_lots=2,
            average_price="100",
            invested_amount="2000",
            currency="RUB",
            observed_at=NOW,
        ),
    )


class Repository:
    def __init__(self) -> None:
        self.cached = []
        self.events = []
        self.active = []
        self.holds = []

    def transition_state(self, **values):
        self.events.append(values)
        self.active = [command(AutomationState.IN_WORK)]

    def list_monitored(self):
        return list(self.active)

    def list_active(self):
        return list(self.active)

    def synchronize_core_state(self, automation_id, **values):
        synced = command(AutomationState(values["state"]))
        self.active = [synced] if synced.state is AutomationState.IN_WORK else []
        return synced

    def hold_active(self, reason, automation_id=None):
        self.holds.append((reason, automation_id))

    def get_active_intent(self, automation_id):
        return None


class Synchronization:
    def __init__(self) -> None:
        self.claimed = False
        self.flushed = 0

    def claim_commands(self, worker_id, limit):
        if self.claimed:
            return []
        self.claimed = True
        return [command()]

    def flush_outbox(self):
        self.flushed += 1
        return True


class Core:
    def __init__(self) -> None:
        self.heartbeats = []
        self.status_batches = []

    def automation_status(self, automation_id):
        raise AssertionError("Coordinator must use the batch status contract")

    def automation_statuses(self, automation_ids):
        self.status_batches.append(tuple(automation_ids))
        return SimpleNamespace(
            automations={
                automation_id: {
                    "state": "IN_WORK",
                    "revision": 2,
                    "last_sequence_number": 1,
                }
                for automation_id in automation_ids
            },
            missing_automation_ids=(),
        )

    def broker_connection(self, broker_id):
        return BrokerConnection(str(broker_id), "TINVEST_SANDBOX", "sandbox", "synthetic-token", True)

    def heartbeat(self, worker_id, occurred_at):
        self.heartbeat_value = (worker_id, occurred_at)
        self.heartbeats.append((worker_id, occurred_at))


class Bundle:
    def __init__(self) -> None:
        self.broker_id = "broker"
        self.commands = []
        self.closed = False
        self.release = asyncio.Event()
        self.runtime = self

    async def replace_commands(self, commands):
        self.commands.append(commands)

    async def run(self):
        await self.release.wait()

    async def close(self):
        self.closed = True
        self.release.set()


def test_claims_syncs_and_assigns_commands_to_one_broker_runtime() -> None:
    async def scenario():
        repository = Repository()
        synchronization = Synchronization()
        core = Core()
        bundle = Bundle()

        async def builder(connection):
            return bundle

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.close()
        return repository, synchronization, core, bundle

    repository, synchronization, core, bundle = asyncio.run(scenario())

    assert repository.events[0]["state"] == "IN_WORK"
    assert bundle.commands[0][0].state is AutomationState.IN_WORK
    assert core.heartbeat_value == ("worker", NOW)
    assert core.status_batches == [(str(command().automation_id),)]
    assert bundle.closed


@pytest.mark.parametrize("local_state", [AutomationState.HOLD, AutomationState.IN_QUEUE, AutomationState.IN_WORK])
@pytest.mark.parametrize("core_state", ["HOLD", "CLOSED", "IN_QUEUE", None])
def test_restored_bootstrap_hold_is_prepared_without_claim_or_early_activation(local_state, core_state) -> None:
    class BootstrapRepository(Repository):
        def __init__(self):
            super().__init__()
            self.active = [bootstrap_command().model_copy(update={"state": local_state})]

        def list_active(self):
            return [value for value in self.active if value.state is not AutomationState.HOLD]

        def synchronize_core_state(self, automation_id, **values):
            return self.active[0]

    class HoldCore(Core):
        def automation_statuses(self, automation_ids):
            result = super().automation_statuses(automation_ids)
            if core_state is None:
                return SimpleNamespace(automations={}, missing_automation_ids=tuple(automation_ids))
            for value in result.automations.values():
                value["state"] = core_state
            return result

    async def scenario():
        repository = BootstrapRepository()
        synchronization = Synchronization()
        synchronization.claimed = True
        core = HoldCore()
        bundle = Bundle()

        async def builder(connection):
            return bundle

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.run_iteration()
        await service.close()
        return repository, bundle

    repository, bundle = asyncio.run(scenario())

    assert repository.events == []
    expected = (
        [(bootstrap_command().model_copy(update={"state": AutomationState(core_state)}),)] * 2
        if core_state in {"HOLD", "IN_QUEUE"}
        else []
    )
    assert bundle.commands == expected


def test_closed_adopted_automation_keeps_uncertain_intent_supervision_after_bootstrap_ack(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'closed-adopted.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = LocalAutomationRepository(factory)
    value = bootstrap_command()
    automation_id = str(value.automation_id)
    repository.cache_command(value)
    PositionBootstrapService(repository).ensure(value, StrategySettings())
    repository.acknowledge_fact_outbox(automation_id, accepted_through_sequence=4, current_revision=2)
    persisted = repository.save_decision_batch((decision_item(value),), occurred_at=NOW)
    intent_id = persisted.intents[0].idempotency_key
    repository.update_intent(intent_id, state="UNCERTAIN", occurred_at=NOW)

    class ClosedCore(Core):
        def automation_statuses(self, automation_ids):
            result = super().automation_statuses(automation_ids)
            for status in result.automations.values():
                status["state"] = "CLOSED"
            return result

    async def scenario():
        synchronization = Synchronization()
        synchronization.claimed = True
        bundle = Bundle()

        async def builder(connection):
            return bundle

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            ClosedCore(),
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.close()
        return bundle

    bundle = asyncio.run(scenario())
    assert len(bundle.commands) == 1
    assert bundle.commands[0][0].state is AutomationState.CLOSED
    assert str(bundle.commands[0][0].automation_id) == automation_id
    assert repository.get_active_intent(automation_id).state == "UNCERTAIN"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalIntentModel)) == 1
    engine.dispose()


def test_flush_makes_one_decision_when_outbox_batch_is_below_deadline() -> None:
    class BelowDeadlineSynchronization(Synchronization):
        def flush_outbox(self):
            self.flushed += 1
            return True

    async def scenario():
        repository = Repository()
        synchronization = BelowDeadlineSynchronization()
        synchronization.claimed = True
        core = Core()

        async def builder(connection):
            return Bundle()

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.close()
        return synchronization.flushed

    assert asyncio.run(scenario()) == 2


def test_heartbeat_is_sent_no_more_than_once_per_three_seconds() -> None:
    async def scenario():
        monotonic_value = 0.0
        repository = Repository()
        synchronization = Synchronization()
        synchronization.claimed = True
        core = Core()

        async def builder(connection):
            return Bundle()

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
            monotonic=lambda: monotonic_value,
            heartbeat_interval_seconds=3.0,
        )
        for value in (0.0, 1.0, 2.0, 3.0, 4.0):
            monotonic_value = value
            await service.run_iteration()
            await asyncio.sleep(0)
        await service.close()
        return core

    core = asyncio.run(scenario())

    assert len(core.heartbeats) == 2


def test_hold_is_synchronized_but_not_assigned_to_broker_runtime() -> None:
    class HoldRepository(Repository):
        def synchronize_core_state(self, automation_id, **values):
            synced = command(AutomationState(values["state"]))
            self.active = [synced]
            return synced

        def list_active(self):
            return [item for item in self.active if item.state is AutomationState.IN_WORK]

    async def scenario():
        repository = HoldRepository()
        repository.active = [command(AutomationState.HOLD)]
        synchronization = Synchronization()
        synchronization.claimed = True
        core = Core()
        core.automation_statuses = lambda automation_ids: SimpleNamespace(
            automations={
                automation_id: {
                    "state": "HOLD",
                    "revision": 2,
                    "last_sequence_number": 1,
                }
                for automation_id in automation_ids
            },
            missing_automation_ids=(),
        )
        builds = []

        async def builder(connection):
            builds.append(connection)
            return Bundle()

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.close()
        return builds

    assert asyncio.run(scenario()) == []


def test_rebuilds_broker_runtime_after_its_task_finishes_with_error() -> None:
    class FailingBundle(Bundle):
        async def run(self):
            cause = ValueError("UNIQUE constraint failed: fact_outbox.automation_id, fact_outbox.sequence_number")
            cause.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"  # type: ignore[attr-defined]
            raise RuntimeError("broker runtime failed") from cause

    async def scenario():
        repository = Repository()
        synchronization = Synchronization()
        core = Core()
        first = FailingBundle()
        second = Bundle()
        bundles = iter((first, second))

        async def builder(connection):
            return next(bundles)

        service = StreamingRuntimeCoordinatorService(
            repository,
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await asyncio.sleep(0)
        await service.run_iteration()
        await service.close()
        return first, second

    records: list[logging.LogRecord] = []

    class RecordHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = RecordHandler()
    was_disabled = LOGGER.disabled
    LOGGER.disabled = False
    LOGGER.addHandler(handler)
    try:
        first, second = asyncio.run(scenario())
    finally:
        LOGGER.removeHandler(handler)
        LOGGER.disabled = was_disabled

    assert first.closed is True
    assert len(second.commands) == 1
    messages = [record.getMessage() for record in records]
    assert any("Broker runtime stopped unexpectedly" in message for message in messages)
    assert any("RuntimeError" in message for message in messages)
    assert any("ValueError" in message for message in messages)
    assert any("SQLITE_CONSTRAINT_UNIQUE" in message for message in messages)
    assert any("uq_fact_outbox_sequence" in message for message in messages)
