from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import trading_automaton.storage.execution_diagnostics as diagnostics
from sentinel_contracts.broker_execution import BrokerOrderStatus
from sentinel_contracts.worker_diagnostics import WorkerExecutionDiagnostics
from tests.trading_automaton.command_factory import command
from trading_automaton.storage.execution_diagnostics import WorkerExecutionDiagnosticsReader
from trading_automaton.storage.models import Base, LocalIntentModel
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000102")


def worker_database(path: Path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = command(broker=str(BROKER_ID))
    repository.cache_command(value)
    engine.dispose()
    return value


def test_reader_does_not_checkout_the_main_pool(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    value = worker_database(path)
    main = create_engine(f"sqlite:///{path}", pool_size=1, max_overflow=0)
    try:
        with main.connect():
            started = monotonic()
            result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
            elapsed = monotonic() - started

        assert result.observation == "OBSERVED"
        assert result.rows[0].automation_id == value.automation_id
        assert elapsed < 0.1
    finally:
        main.dispose()


def test_reader_keeps_active_intent_first_and_truncates_terminal_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    value = worker_database(path)
    engine = create_engine(f"sqlite:///{path}")
    terminal_states = (BrokerOrderStatus.CANCELLED,) * 5
    with engine.begin() as connection:
        states = (BrokerOrderStatus.FAILED, BrokerOrderStatus.FILLED, *terminal_states)
        for index, state in enumerate(states):
            connection.execute(
                LocalIntentModel.__table__.insert().values(
                    idempotency_key=str(UUID(int=index + 1)),
                    fact_execution_id=None,
                    position_cycle_id=None,
                    automation_id=str(value.automation_id),
                    kind="BUY_MORE",
                    side="BUY",
                    state=state,
                    quantity_lots=1,
                    limit_price=Decimal("10"),
                    broker_order_id=None,
                    requested_amount=Decimal("10"),
                    executed_amount=Decimal("0"),
                    estimated_commission=Decimal("0"),
                    executed_commission=Decimal("0"),
                    executed_lots=0,
                    executed_price=Decimal("7.5"),
                    execution_currency="RUB",
                    executed_at=None,
                    dispatch_started_at=None,
                    broker_responded_at=None,
                    terminal_at=None if index == 0 else NOW,
                    execution_facts_emitted_at=None,
                    strategy_snapshot={
                        "admission_origin": {
                            "runtime_instance_id": "00000000-0000-4000-8000-000000000201",
                            "generation": 3,
                        },
                        "private_strategy_value": "must-not-leak",
                    },
                    created_at=NOW,
                    updated_at=NOW.replace(second=index),
                )
            )
        connection.execute(
            Base.metadata.tables["trade_decisions"].insert(),
            [
                {
                    "id": str(UUID(int=301)),
                    "process_id": str(UUID(int=401)),
                    "automation_id": str(value.automation_id),
                    "broker_id": str(value.broker_id),
                    "instrument_id": str(value.instrument_id),
                    "occurred_at": NOW - timedelta(minutes=1),
                    "quantity_lots": 1,
                    "lot_size": 1,
                    "average_price": Decimal("10"),
                    "current_price": Decimal("10"),
                    "best_bid": Decimal("9.9"),
                    "best_ask": Decimal("10.1"),
                    "invested_amount": Decimal("10"),
                    "estimated_commission": Decimal("0.1"),
                    "decision": "WAIT",
                    "reason_code": "OLDER",
                    "decision_quantity_lots": 1,
                    "limit_price": Decimal("10"),
                    "strategy_snapshot": {},
                    "intent_id": str(UUID(int=1)),
                },
                {
                    "id": str(UUID(int=302)),
                    "process_id": str(UUID(int=402)),
                    "automation_id": str(value.automation_id),
                    "broker_id": str(value.broker_id),
                    "instrument_id": str(value.instrument_id),
                    "occurred_at": NOW,
                    "quantity_lots": 1,
                    "lot_size": 1,
                    "average_price": Decimal("10"),
                    "current_price": Decimal("10"),
                    "best_bid": Decimal("9.9"),
                    "best_ask": Decimal("10.1"),
                    "invested_amount": Decimal("10"),
                    "estimated_commission": Decimal("0.1"),
                    "decision": "WAIT",
                    "reason_code": "LATEST",
                    "decision_quantity_lots": 1,
                    "limit_price": Decimal("10"),
                    "strategy_snapshot": {},
                    "intent_id": str(UUID(int=1)),
                },
            ],
        )
    engine.dispose()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="PROD").read(value.broker_id, NOW)

    assert result.observation == "OBSERVED", result
    intents = result.rows[0].intents
    assert len(intents) == 5
    assert intents[0].active is True
    assert intents[0].decision_id == UUID(int=302)
    assert intents[0].process_id == UUID(int=402)
    assert intents[0].limit_price == Decimal("10")
    assert intents[0].executed_price == Decimal("7.5")
    assert intents[0].origin_generation == 3
    assert intents[0].origin_runtime_instance_id == UUID("00000000-0000-4000-8000-000000000201")
    assert result.rows[0].history_truncated is True
    assert "private_strategy_value" not in result.model_dump(mode="json").__str__()

    test_result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="TEST").read(value.broker_id, NOW)
    assert test_result.rows[0].intents[0].active is True
    filled = next(intent for intent in test_result.rows[0].intents if intent.state == "FILLED")
    assert filled.active is True


def test_reader_does_not_report_age_expired_failed_order_as_active(tmp_path: Path) -> None:
    path = tmp_path / "age-expired.sqlite"
    value = worker_database(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            LocalIntentModel.__table__.insert().values(
                idempotency_key=str(UUID(int=2002)),
                fact_execution_id=None,
                position_cycle_id=None,
                automation_id=str(value.automation_id),
                kind="BUY_MORE",
                side="BUY",
                state=BrokerOrderStatus.FAILED,
                quantity_lots=1,
                limit_price=Decimal("10"),
                broker_order_id=None,
                requested_amount=Decimal("10"),
                executed_amount=Decimal("0"),
                estimated_commission=Decimal("0"),
                executed_commission=Decimal("0"),
                executed_lots=0,
                executed_price=Decimal("0"),
                execution_currency="RUB",
                executed_at=None,
                dispatch_started_at=None,
                broker_responded_at=None,
                terminal_at=None,
                execution_facts_emitted_at=None,
                strategy_snapshot={
                    "_age_policy": {
                        "version": 1,
                        "expired_at": NOW.isoformat(timespec="milliseconds"),
                    }
                },
                created_at=NOW - timedelta(hours=25),
                updated_at=NOW,
            )
        )
    engine.dispose()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="PROD").read(value.broker_id, NOW)

    assert result.observation == "OBSERVED", result
    aged = next(intent for intent in result.rows[0].intents if intent.intent_id == UUID(int=2002))
    assert aged.state == "FAILED"
    assert aged.active is False


def test_reader_applies_worker_numeric_scale_to_fractional_commission(tmp_path: Path) -> None:
    path = tmp_path / "fractional-commission.sqlite"
    value = worker_database(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            LocalIntentModel.__table__.insert().values(
                idempotency_key=str(UUID(int=2001)),
                fact_execution_id=None,
                position_cycle_id=None,
                automation_id=str(value.automation_id),
                kind="BUY_MORE",
                side="BUY",
                state=BrokerOrderStatus.UNCERTAIN,
                quantity_lots=1,
                limit_price=Decimal("2.845000000"),
                broker_order_id=None,
                requested_amount=Decimal("51.210000000"),
                executed_amount=Decimal("0"),
                estimated_commission=Decimal("0.1809540644625"),
                executed_commission=Decimal("0"),
                executed_lots=0,
                executed_price=Decimal("0"),
                execution_currency="RUB",
                executed_at=None,
                dispatch_started_at=None,
                broker_responded_at=None,
                terminal_at=None,
                execution_facts_emitted_at=None,
                strategy_snapshot={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    engine.dispose()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="PROD").read(value.broker_id, NOW)

    assert result.observation == "OBSERVED", result
    assert result.rows[0].intents[0].estimated_commission == Decimal("0.180954064")


@pytest.mark.parametrize(
    ("environment", "state", "terminal_at", "execution_emitted_at", "newer_count", "expected_active"),
    [
        ("PROD", BrokerOrderStatus.CANCELLED, None, None, 5, True),
        ("PROD", BrokerOrderStatus.REJECTED, None, None, 4, True),
        ("PROD", BrokerOrderStatus.EXPIRED, None, None, 4, True),
        ("TEST", BrokerOrderStatus.CANCELLED, None, None, 4, False),
        ("TEST", BrokerOrderStatus.REJECTED, None, None, 4, False),
        ("TEST", BrokerOrderStatus.EXPIRED, None, None, 4, False),
        ("TEST", BrokerOrderStatus.FILLED, NOW, None, 4, True),
        ("TEST", BrokerOrderStatus.FAILED, NOW, None, 4, False),
    ],
)
def test_environment_active_policy_prioritizes_only_unfinalized_intents(
    tmp_path: Path,
    environment: str,
    state: BrokerOrderStatus,
    terminal_at: datetime | None,
    execution_emitted_at: datetime | None,
    newer_count: int,
    expected_active: bool,
) -> None:
    path = tmp_path / f"active-policy-{environment}-{state.value}.sqlite"
    value = worker_database(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            LocalIntentModel.__table__.insert().values(
                idempotency_key=str(UUID(int=1000)),
                fact_execution_id=None,
                position_cycle_id=None,
                automation_id=str(value.automation_id),
                kind="BUY_MORE",
                side="BUY",
                state=state,
                quantity_lots=1,
                limit_price=Decimal("10"),
                broker_order_id=None,
                requested_amount=Decimal("10"),
                executed_amount=Decimal("0"),
                estimated_commission=Decimal("0"),
                executed_commission=Decimal("0"),
                executed_lots=0,
                executed_price=Decimal("0"),
                execution_currency="RUB",
                executed_at=None,
                dispatch_started_at=None,
                broker_responded_at=None,
                terminal_at=terminal_at,
                execution_facts_emitted_at=execution_emitted_at,
                strategy_snapshot={},
                created_at=NOW.replace(minute=0),
                updated_at=NOW.replace(minute=0),
            )
        )
        for index in range(newer_count):
            connection.execute(
                LocalIntentModel.__table__.insert().values(
                    idempotency_key=str(UUID(int=1100 + index)),
                    fact_execution_id=None,
                    position_cycle_id=None,
                    automation_id=str(value.automation_id),
                    kind="BUY_MORE",
                    side="BUY",
                    state=BrokerOrderStatus.CANCELLED,
                    quantity_lots=1,
                    limit_price=Decimal("10"),
                    broker_order_id=None,
                    requested_amount=Decimal("10"),
                    executed_amount=Decimal("0"),
                    estimated_commission=Decimal("0"),
                    executed_commission=Decimal("0"),
                    executed_lots=0,
                    executed_price=Decimal("0"),
                    execution_currency="RUB",
                    executed_at=None,
                    dispatch_started_at=None,
                    broker_responded_at=None,
                    terminal_at=NOW,
                    execution_facts_emitted_at=NOW,
                    strategy_snapshot={},
                    created_at=NOW,
                    updated_at=NOW.replace(second=index + 1),
                )
            )
    engine.dispose()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment=environment).read(value.broker_id, NOW)

    assert result.observation == "OBSERVED", result
    target = next(intent for intent in result.rows[0].intents if intent.intent_id == UUID(int=1000))
    assert target.active is expected_active
    if expected_active and newer_count == 5:
        assert result.rows[0].intents[0].intent_id == UUID(int=1000)


def test_reader_bounds_automation_rows_at_one_hundred(tmp_path: Path) -> None:
    path = tmp_path / "many.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = None
    for index in range(101):
        value = command(automation=f"many-{index}", broker=str(BROKER_ID))
        repository.cache_command(value)
    engine.dispose()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)

    assert result.observation == "OBSERVED"
    assert len(result.rows) == 100
    assert result.truncated is True


def test_expensive_query_is_interrupted_without_partial_projection(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "expensive.sqlite"
    value = worker_database(path)
    monkeypatch.setattr(
        diagnostics,
        "_INTENTS",
        text(
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000"
            ") SELECT value FROM counter"
        ),
    )
    started = monotonic()

    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)

    assert result.observation == "UNKNOWN"
    assert result.reason == "BUDGET_EXCEEDED"
    assert result.rows == ()
    assert monotonic() - started < 0.25


def test_connections_are_closed_after_timeout_and_repeated_success(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "cleanup.sqlite"
    value = worker_database(path)
    opened = 0
    closed = 0
    create_engine = diagnostics.create_engine

    def tracked_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)

        def on_connect(_dbapi_connection, _record):
            nonlocal opened
            opened += 1

        def on_close(_dbapi_connection, _record):
            nonlocal closed
            closed += 1

        event.listen(engine, "connect", on_connect)
        event.listen(engine, "close", on_close)
        return engine

    monkeypatch.setattr(diagnostics, "create_engine", tracked_engine)
    reader = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}")
    assert reader.read(value.broker_id, NOW).observation == "OBSERVED"
    with monkeypatch.context() as timeout_patch:
        timeout_patch.setattr(
            diagnostics,
            "_INTENTS",
            text(
                "WITH RECURSIVE counter(value) AS ("
                "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000"
                ") SELECT value FROM counter"
            ),
        )
        timed_out = reader.read(value.broker_id, NOW)
    assert timed_out.observation == "UNKNOWN"
    assert timed_out.reason == "BUDGET_EXCEEDED"
    assert reader.read(value.broker_id, NOW).observation == "OBSERVED"

    assert opened == closed == 3


def test_missing_and_memory_sources_fail_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "must-not-exist.sqlite"

    missing = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(BROKER_ID, NOW)
    memory = WorkerExecutionDiagnosticsReader("sqlite:///:memory:").read(BROKER_ID, NOW)

    assert missing.observation == memory.observation == "UNKNOWN"
    assert missing.reason == memory.reason == "UNSUPPORTED_SOURCE"
    assert not path.exists()


def test_exclusive_writer_lock_returns_unknown_within_budget(tmp_path: Path) -> None:
    path = tmp_path / "locked.sqlite"
    value = worker_database(path)
    writer = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
    try:
        with writer.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=DELETE")
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            started = monotonic()
            result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
            elapsed = monotonic() - started

        assert result.observation == "UNKNOWN"
        assert result.reason == "BUDGET_EXCEEDED"
        assert result.rows == ()
        assert elapsed < 0.2
    finally:
        writer.dispose()


def test_reader_result_type_is_the_shared_safe_projection() -> None:
    assert WorkerExecutionDiagnosticsReader
    assert WorkerExecutionDiagnostics
