"""Acceptance of Worker-local durability without persisting hot market data."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.automation_lifecycle import InvalidAutomationTransition
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import FactKind
from tests.trading_automaton.command_factory import command, decision_item
from trading_automaton.services.recovery import RecoveryService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).parents[2]


def baseline_command():
    return command(broker="broker-1", account="account-1", instrument="instrument-1")


def open_repository(path) -> tuple[Engine, LocalAutomationRepository]:
    engine = create_worker_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))


def test_worker_schema_contains_no_hot_market_or_hydrated_position_snapshots(tmp_path) -> None:
    engine, _repository = open_repository(tmp_path / "worker.db")

    table_names = set(inspect(engine).get_table_names())

    assert table_names.isdisjoint(
        {"market_snapshots", "order_books", "candles", "hot_market_data", "hydrated_position_states"}
    )
    engine.dispose()


def test_typed_worker_fact_paths_do_not_import_core_domain_or_storage() -> None:
    paths = [
        PROJECT_ROOT / "src/sentinel_contracts/trading_facts.py",
        PROJECT_ROOT / "src/trading_automaton/storage/fact_outbox.py",
        PROJECT_ROOT / "src/trading_automaton/services/fact_synchronization.py",
    ]
    forbidden = (
        "moex_sentinel.domain",
        "moex_sentinel.storage.models",
        "moex_sentinel.storage.repositories",
    )

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ]
        assert not any(module.startswith(forbidden) for module in imports), path


def test_pending_intent_and_typed_outbox_survive_repository_recreation(tmp_path) -> None:
    database_path = tmp_path / "worker.db"
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    engine, repository = open_repository(database_path)
    repository.cache_command(command_value)
    repository.save_decision_batch((decision_item(command_value),), occurred_at=NOW)
    engine.dispose()

    reopened_engine, reopened = open_repository(database_path)
    active = reopened.get_active_intent(automation_id)
    pending = reopened.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)

    assert active is not None
    assert active.idempotency_key == "00000000-0000-4000-8000-000000000405"
    assert [event.fact_kind for event in pending] == [
        FactKind.TRADE_DECISION_RECORDED.value,
        FactKind.BROKER_ORDER_RECORDED.value,
    ]
    reopened_engine.dispose()


def test_acknowledged_terminal_fact_is_compacted_but_state_survives(tmp_path) -> None:
    database_path = tmp_path / "worker.db"
    command_value = baseline_command()
    automation_id = str(command_value.automation_id)
    engine, repository = open_repository(database_path)
    repository.cache_command(command_value)
    repository.transition_state(
        automation_id=automation_id,
        state=AutomationState.CLOSED.value,
        safe_message="Automation closed",
        occurred_at=NOW,
    )
    pending = repository.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)
    repository.acknowledge_fact_outbox(
        automation_id,
        accepted_through_sequence=pending[-1].sequence_number,
        current_revision=2,
    )
    engine.dispose()

    reopened_engine, reopened = open_repository(database_path)

    assert reopened.has_pending_fact_outbox(automation_id) is False
    assert reopened.get_state(automation_id).state == AutomationState.CLOSED.value
    assert reopened.get_state(automation_id).revision == 2
    reopened_engine.dispose()


def test_unclean_restart_restores_fact_outbox_then_requires_manual_resume(tmp_path) -> None:
    database_path = tmp_path / "worker.db"
    command_value = baseline_command().model_copy(update={"state": AutomationState.IN_QUEUE})
    automation_id = str(command_value.automation_id)
    engine, repository = open_repository(database_path)
    repository.cache_command(command_value)
    repository.transition_state(
        automation_id=automation_id,
        state=AutomationState.IN_WORK.value,
        safe_message="Position snapshot",
        occurred_at=NOW,
    )
    activation = repository.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)
    assert len(activation) == 1
    assert activation[0].payload["state"] == "IN_WORK"
    assert activation[0].expected_revision == 1
    assert repository.begin_run("worker-1") is False
    engine.dispose()

    reopened_engine, reopened = open_repository(database_path)
    restored = reopened.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)
    assert restored == activation
    unclean_shutdown = reopened.begin_run("worker-1")
    assert unclean_shutdown is True
    RecoveryService(reopened).recover(unclean_shutdown=unclean_shutdown)
    pending = reopened.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0)

    assert [event.sequence_number for event in pending] == [1, 2]
    assert pending[0] == activation[0]
    assert [event.payload["state"] for event in pending] == ["IN_WORK", "HOLD"]
    assert [event.expected_revision for event in pending] == [1, 2]
    assert reopened.get_state(automation_id).state == AutomationState.HOLD.value
    assert reopened.get_state(automation_id).revision == 3
    assert reopened.list_active() == []
    with pytest.raises(InvalidAutomationTransition):
        reopened.transition_state(
            automation_id=automation_id,
            state=AutomationState.IN_WORK.value,
            safe_message="Unrequested automatic restart",
            occurred_at=NOW,
        )
    RecoveryService(reopened).recover(unclean_shutdown=True)
    assert reopened.ready_fact_outbox(10, now=datetime.now(UTC), deadline_ms=0) == pending

    manual_resume = command_value.model_copy(update={"revision": 4, "last_sequence_number": 2})
    assert reopened.cache_command(manual_resume) is False
    reopened.acknowledge_fact_outbox(automation_id, accepted_through_sequence=2, current_revision=3)
    assert reopened.has_pending_fact_outbox(automation_id) is False
    assert reopened.get_state(automation_id).state == AutomationState.HOLD.value
    assert reopened.cache_command(manual_resume) is True
    assert reopened.get_state(automation_id).state == AutomationState.IN_QUEUE.value
    assert reopened.get_state(automation_id).revision == 4
    reopened_engine.dispose()
