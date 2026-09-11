import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import moex_sentinel.portfolio_snapshot_worker as worker_module
from moex_sentinel.composition import build_portfolio_snapshot_collector
from moex_sentinel.config import Settings
from moex_sentinel.portfolio_snapshot_worker import run_forever
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository


class Collector:
    def __init__(self) -> None:
        self.calls = 0

    async def collect_once(self) -> None:
        self.calls += 1


def test_worker_collects_immediately_then_waits_for_interval() -> None:
    collector = Collector()
    stop = asyncio.Event()
    waits: list[int] = []

    async def wait(_stop: asyncio.Event, seconds: int) -> None:
        waits.append(seconds)
        stop.set()

    asyncio.run(run_forever(collector, interval_seconds=60, stop=stop, wait=wait))

    assert collector.calls == 1
    assert waits == [60]


def test_worker_continues_after_unexpected_collection_failure() -> None:
    class FlakyCollector(Collector):
        async def collect_once(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic failure")

    collector = FlakyCollector()
    stop = asyncio.Event()

    async def wait(_stop: asyncio.Event, _seconds: int) -> None:
        if collector.calls == 2:
            stop.set()

    asyncio.run(run_forever(collector, interval_seconds=60, stop=stop, wait=wait))

    assert collector.calls == 2


def test_snapshot_collector_composition_persists_an_empty_run() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    settings = Settings(_env_file=None)
    collector = build_portfolio_snapshot_collector(
        factory,
        engine,
        settings,
        clock=lambda: datetime(2026, 8, 15, 10, tzinfo=UTC),
    )

    result = asyncio.run(collector.collect_once())

    with factory() as session:
        persisted = PortfolioSnapshotRepository(session).latest_run()
    engine.dispose()
    assert result.saved == 0
    assert persisted is not None
    assert persisted.id == result.run_id


def test_worker_run_disposes_configured_database_engine(monkeypatch) -> None:
    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    settings = SimpleNamespace(
        database_url="sqlite:///:memory:",
        database_pool_size=4,
        database_max_overflow=2,
        database_pool_timeout_seconds=3.5,
        portfolio_snapshot_interval_seconds=60,
        log_level="INFO",
        log_format="console",
    )

    def create_engine(
        database_url: str,
        *,
        pool_size: int,
        max_overflow: int,
        pool_timeout_seconds: float,
    ) -> Engine:
        assert (database_url, pool_size, max_overflow, pool_timeout_seconds) == (
            "sqlite:///:memory:",
            4,
            2,
            3.5,
        )
        return engine

    async def finish_immediately(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(worker_module, "Settings", lambda: settings)
    monkeypatch.setattr(worker_module, "create_database_engine", create_engine)
    monkeypatch.setattr(worker_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(worker_module, "build_portfolio_snapshot_collector", lambda *_args: Collector())
    monkeypatch.setattr(worker_module, "run_forever", finish_immediately)
    monkeypatch.setattr(worker_module, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_module, "business_process", nullcontext)
    monkeypatch.setattr(worker_module, "audit_event", lambda *_args, **_kwargs: None)

    asyncio.run(worker_module.run())

    assert engine.disposed is True
