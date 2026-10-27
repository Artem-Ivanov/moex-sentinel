import asyncio
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.trading_automaton.command_factory import command
from trading_automaton.runtime.streaming_coordinator import StreamingRuntimeCoordinator
from trading_automaton.storage.execution_diagnostics import WorkerExecutionDiagnosticsReader
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
BROKER_ID = UUID("00000000-0000-4000-8000-000000000302")


def test_sqlite_lock_still_delivers_heartbeat_and_allows_coordinator_close(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    value = command(broker=str(BROKER_ID))
    LocalAutomationRepository(sessionmaker(engine)).cache_command(value)
    engine.dispose()

    reader = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}")
    heartbeats = []
    coordinator = StreamingRuntimeCoordinator(
        object(),
        type(
            "Core",
            (),
            {"heartbeat": lambda _self, _worker, _at, diagnostics: heartbeats.append(diagnostics)},
        )(),
        lambda _: None,
        worker_id="worker",
        now=lambda: NOW,
        execution_diagnostics=lambda captured_at: reader.read(BROKER_ID, captured_at),
    )
    coordinator._last_finished_at = NOW
    coordinator._last_result = "OUTBOX_BLOCKED"

    lock_engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})

    async def send_and_close():
        with lock_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=DELETE")
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            started = monotonic()
            coordinator._schedule_heartbeat()
            await asyncio.wait_for(coordinator.close(), timeout=0.3)
            elapsed = monotonic() - started
        return elapsed

    try:
        elapsed = asyncio.run(send_and_close())
    finally:
        lock_engine.dispose()

    assert elapsed < 0.3
    assert len(heartbeats) == 1
    assert heartbeats[0].execution_diagnostics.observation == "UNKNOWN"
    assert heartbeats[0].execution_diagnostics.reason == "BUDGET_EXCEEDED"
