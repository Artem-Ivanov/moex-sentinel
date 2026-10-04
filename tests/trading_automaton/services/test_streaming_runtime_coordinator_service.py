import asyncio
import logging
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic as system_monotonic
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
from trading_automaton.runtime.streaming_coordinator import LOGGER, StreamingRuntimeCoordinator
from trading_automaton.services.automation_lifecycle import WorkerAutomationLifecycleService
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.storage.models import Base, LocalIntentModel
from trading_automaton.storage.repository import LocalAutomationRepository
from trading_automaton.usecases.synchronize_runtime import SynchronizeTradingRuntimeUsecase

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

    def heartbeat(self, worker_id, occurred_at, diagnostics=None):
        self.heartbeat_value = (worker_id, occurred_at)
        self.heartbeats.append((worker_id, occurred_at))
        self.diagnostics = diagnostics


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


def build_coordinator(
    repository,
    synchronization,
    core,
    builder,
    *,
    worker_id,
    now,
    monotonic=system_monotonic,
    heartbeat_interval_seconds=3.0,
    command_limit=100,
):
    """Assemble the real control operation and lifecycle with fresh scenario state."""
    iteration = SynchronizeTradingRuntimeUsecase(
        repository,
        synchronization,
        core,
        WorkerAutomationLifecycleService(repository),
        worker_id=worker_id,
        now=now,
        command_limit=command_limit,
    )
    return StreamingRuntimeCoordinator(
        iteration,
        core,
        builder,
        worker_id=worker_id,
        now=now,
        monotonic=monotonic,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


def test_claims_syncs_and_assigns_commands_to_one_broker_runtime() -> None:
    async def scenario():
        repository = Repository()
        synchronization = Synchronization()
        core = Core()
        bundle = Bundle()

        async def builder(connection):
            return bundle

        service = build_coordinator(
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

        service = build_coordinator(
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

        service = build_coordinator(
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

        service = build_coordinator(
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

        service = build_coordinator(
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

        service = build_coordinator(
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

        service = build_coordinator(
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


def test_held_nonbootstrap_automation_keeps_uncertain_intent_supervision(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'held.db'}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = command(AutomationState.IN_WORK)
    repository.cache_command(value)
    automation_id = str(value.automation_id)
    intent_id = repository.save_decision_batch((decision_item(value),), occurred_at=NOW).intents[0].idempotency_key
    repository.update_intent(intent_id, state="UNCERTAIN", occurred_at=NOW)
    repository.hold_active("Uncertain order", automation_id)

    class HeldCore(Core):
        def automation_statuses(self, automation_ids):
            result = super().automation_statuses(automation_ids)
            for status in result.automations.values():
                status["state"] = "HOLD"
            return result

    async def scenario():
        synchronization = Synchronization()
        synchronization.claimed = True
        bundle = Bundle()

        async def builder(connection):
            return bundle

        service = build_coordinator(
            repository,
            synchronization,
            HeldCore(),
            builder,
            worker_id="worker",
            now=lambda: NOW,
        )
        await service.run_iteration()
        await service.close()
        return bundle

    bundle = asyncio.run(scenario())
    assert len(bundle.commands) == 1
    assert bundle.commands[0][0].state is AutomationState.HOLD
    assert repository.get_active_intent(automation_id).state == "UNCERTAIN"
    engine.dispose()


@pytest.mark.parametrize("blocked_flush", [1, 2])
def test_each_outbox_block_sends_heartbeat_without_successful_progress(blocked_flush):
    class Blocked(Synchronization):
        def flush_outbox(self):
            self.flushed += 1
            return self.flushed != blocked_flush

    async def scenario():
        core = Core()

        async def builder(_connection):
            return Bundle()

        service = build_coordinator(Repository(), Blocked(), core, builder, worker_id="worker", now=lambda: NOW)
        await service.run_iteration()
        await service.close()
        return core

    core = asyncio.run(scenario())
    assert len(core.heartbeats) == 1
    assert core.diagnostics.completed_iterations == 0
    assert core.diagnostics.last_completed_at is None
    assert core.diagnostics.last_result == "OUTBOX_BLOCKED"


def test_iteration_exception_reports_safe_error_and_preserves_original_exception():
    failure = ValueError("synthetic-secret")

    class Failed(Synchronization):
        def flush_outbox(self):
            raise failure

    async def scenario():
        core = Core()

        async def builder(_connection):
            return Bundle()

        service = build_coordinator(Repository(), Failed(), core, builder, worker_id="worker", now=lambda: NOW)
        with pytest.raises(ValueError, match="synthetic-secret") as caught:
            await service.run_iteration()
        assert caught.value is failure
        await service.close()
        return core

    core = asyncio.run(scenario())
    assert core.diagnostics.last_result == "ERROR"
    assert core.diagnostics.error_code == "ITERATION_FAILED"
    assert "synthetic-secret" not in core.diagnostics.model_dump_json()


def test_completed_control_iteration_increments_progress_and_hb_failure_is_consumed(caplog, monkeypatch):
    # Alembic's fileConfig disables existing loggers in preceding migration tests.
    monkeypatch.setattr(LOGGER, "disabled", False)

    class FailedHeartbeat(Core):
        def heartbeat(self, worker_id, occurred_at, diagnostics=None):
            super().heartbeat(worker_id, occurred_at, diagnostics)
            raise ValueError("heartbeat-secret")

    async def scenario():
        core = FailedHeartbeat()

        async def builder(_connection):
            return Bundle()

        service = build_coordinator(Repository(), Synchronization(), core, builder, worker_id="worker", now=lambda: NOW)
        await service.run_iteration()
        await service.close()
        return core

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        core = asyncio.run(scenario())
    assert core.diagnostics.completed_iterations == 1
    assert core.diagnostics.last_result == "COMPLETED"
    assert core.diagnostics.last_completed_at == NOW
    assert "Heartbeat delivery failed: ValueError" in caplog.text
    assert "heartbeat-secret" not in caplog.text


def test_cancelled_hanging_iteration_never_claims_completion():
    async def scenario():
        core = Core()
        entered = asyncio.Event()

        async def builder(_connection):
            entered.set()
            await asyncio.Event().wait()

        service = build_coordinator(Repository(), Synchronization(), core, builder, worker_id="worker", now=lambda: NOW)
        task = asyncio.create_task(service.run_iteration())
        await entered.wait()
        assert not core.heartbeats
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await service.close()
        assert not core.heartbeats

    asyncio.run(scenario())


def test_outbox_read_failure_preserves_control_heartbeat_and_interval_bounds():
    samples = []

    def failed_read():
        samples.append(1)
        raise RuntimeError("storage-secret")

    async def scenario():
        core = Core()
        repository = Repository()
        synchronization = Synchronization()
        ticks = [0.0]

        async def builder(_connection):
            return Bundle()

        iteration = SynchronizeTradingRuntimeUsecase(
            repository,
            synchronization,
            core,
            WorkerAutomationLifecycleService(repository),
            worker_id="worker",
            now=lambda: NOW,
        )
        service = StreamingRuntimeCoordinator(
            iteration,
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
            monotonic=lambda: ticks[0],
            outbox_diagnostics=failed_read,
        )
        await service.run_iteration()
        await service._heartbeat_task
        ticks[0] = 1
        await service.run_iteration()
        assert len(samples) == 1
        ticks[0] = 3
        await service.run_iteration()
        await service.close()
        return core

    core = asyncio.run(scenario())
    assert len(samples) == 2
    assert core.diagnostics.completed_iterations == 3
    assert core.diagnostics.outbox.observation == "UNKNOWN"
    assert core.diagnostics.outbox.pending_count is None


def test_slow_heartbeat_remains_single_and_close_waits_for_delivery():
    entered, release = Event(), Event()

    class SlowCore(Core):
        def heartbeat(self, worker_id, occurred_at, diagnostics=None):
            entered.set()
            assert release.wait(timeout=5)
            super().heartbeat(worker_id, occurred_at, diagnostics)

    async def scenario():
        core = SlowCore()
        ticks = [0.0]

        async def builder(_connection):
            return Bundle()

        service = build_coordinator(
            Repository(),
            Synchronization(),
            core,
            builder,
            worker_id="worker",
            now=lambda: NOW,
            monotonic=lambda: ticks[0],
        )
        try:
            await service.run_iteration()
            assert await asyncio.to_thread(entered.wait, 5)
            ticks[0] = 6
            await service.run_iteration()
            assert not core.heartbeats
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await closing
            assert len(core.heartbeats) == 1
            assert core.diagnostics.completed_iterations == 1
        finally:
            release.set()

    asyncio.run(scenario())


def test_cancelled_iteration_after_success_does_not_send_fresh_completed_heartbeat():
    async def scenario():
        core, synchronization = Core(), Synchronization()
        synchronization.claimed = True
        entered = asyncio.Event()
        ticks, clock = [0.0], [NOW]

        async def builder(_connection):
            entered.set()
            await asyncio.Event().wait()

        service = build_coordinator(
            Repository(),
            synchronization,
            core,
            builder,
            worker_id="worker",
            now=lambda: clock[0],
            monotonic=lambda: ticks[0],
        )
        await service.run_iteration()
        await service._heartbeat_task
        synchronization.claimed = False
        ticks[0], clock[0] = 3.0, NOW + timedelta(seconds=3)
        task = asyncio.create_task(service.run_iteration())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await service.close()
        assert len(core.heartbeats) == 1
        assert core.diagnostics.completed_iterations == 1
        assert core.diagnostics.last_completed_at == NOW
        assert core.diagnostics.last_finished_at == NOW

    asyncio.run(scenario())
