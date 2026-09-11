import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerConnection, BrokerPosition
from tests.trading_automaton.services.test_broker_tick_preparation_service import bootstrap_market
from tests.trading_automaton.services.test_streaming_runtime_coordinator_service import bootstrap_command
from trading_automaton import composition
from trading_automaton.composition import BrokerRuntimeBundle, build_broker_runtime, build_streaming_runtime
from trading_automaton.config import AutomatonSettings, StrategySettings
from trading_automaton.domain.dtos import CommissionQuote
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.services.streaming_runtime_coordinator import StreamingRuntimeCoordinatorService
from trading_automaton.storage.models import Base
from trading_automaton.storage.repository import LocalAutomationRepository

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Subscription:
    def subscribe(self, instruments):
        pass

    def unsubscribe(self, instruments):
        pass

    def waiting_close(self, enabled=True):
        return self


class Manager:
    def __init__(self) -> None:
        self.order_book = Subscription()
        self.last_price = Subscription()
        self.candles = Subscription()
        self.info = Subscription()

    def __aiter__(self):
        async def events():
            if False:
                yield None

        return events()

    def stop(self):
        pass


class Session:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.manager = Manager()

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1

    def create_market_data_stream(self):
        raise AssertionError("Worker must consume Analytics instead of opening a market stream")


def repository() -> LocalAutomationRepository:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))


def test_builds_one_started_sdk_session_for_broker_runtime() -> None:
    async def scenario():
        session = Session()
        bundle = await build_broker_runtime(
            BrokerConnection("broker", "TINVEST_SANDBOX", "sandbox-target", "synthetic-token", True),
            repository(),
            strategy_settings=StrategySettings(),
            session_factory=lambda _connection: session,
            now=lambda: NOW,
        )
        await bundle.close()
        return session, bundle

    session, bundle = asyncio.run(scenario())

    assert session.started == 1
    assert session.closed == 1
    assert bundle.broker_id == "broker"


def test_composed_preparation_bootstraps_hold_through_real_recovery_service(tmp_path) -> None:
    class PreparedSession(Session):
        async def get_positions(self, account_id):
            return (BrokerPosition("instrument", Decimal(2), Decimal(100), Decimal(101), "RUB"),)

        async def get_free_cash(self, account_id, currency):
            return Decimal(5000)

        async def get_candles(self, *args):
            return ()

        async def quote(self, request, side):
            return CommissionQuote(Decimal(1010), Decimal(1))

    engine = create_engine(f"sqlite:///{tmp_path / 'composed-worker.db'}")
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = bootstrap_command()
    repo.cache_command(value)

    async def scenario():
        session = PreparedSession()
        bundle = await build_broker_runtime(
            BrokerConnection(str(value.broker_id), "TINVEST_SANDBOX", "sandbox-target", "synthetic-token", True),
            repo,
            strategy_settings=StrategySettings(),
            session_factory=lambda _connection: session,
            now=lambda: NOW,
        )
        try:
            await bundle.runtime._preparation.prepare((value,), bootstrap_market())
        finally:
            await bundle.close()

    asyncio.run(scenario())
    lots = repo.list_open_lots(str(value.automation_id))
    assert len(lots) == 1
    assert lots[0].source == "BROKER_POSITION_BOOTSTRAP"
    assert repo.has_pending_fact_outbox(str(value.automation_id))
    engine.dispose()


def test_bundle_closes_session_when_tracking_wait_fails() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class Tracking:
        def __init__(self) -> None:
            self.waited = False

        async def wait_all(self) -> None:
            self.waited = True
            raise RuntimeError("watcher failed")

    async def scenario():
        runtime = Runtime()
        session = Session()
        tracking = Tracking()
        bundle = BrokerRuntimeBundle("broker", runtime, session, tracking)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="watcher failed"):
            await bundle.close()
        return runtime, session, tracking

    runtime, session, tracking = asyncio.run(scenario())

    assert runtime.closed is True
    assert tracking.waited is True
    assert session.closed == 1


def test_bundle_preserves_tracking_failure_when_session_close_also_fails() -> None:
    class Runtime:
        async def close(self) -> None:
            pass

    class Tracking:
        async def wait_all(self) -> None:
            raise RuntimeError("watcher failed")

    class FailingSession(Session):
        async def close(self) -> None:
            self.closed += 1
            raise RuntimeError("session close failed")

    async def scenario():
        session = FailingSession()
        bundle = BrokerRuntimeBundle("broker", Runtime(), session, Tracking())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="watcher failed") as raised:
            await bundle.close()
        return session, raised.value

    session, error = asyncio.run(scenario())

    assert session.closed == 1
    assert error.__cause__ is not None
    assert str(error.__cause__) == "session close failed"


def test_builds_streaming_coordinator_as_production_runtime(tmp_path) -> None:
    settings = AutomatonSettings(
        CORE_URL="http://core.invalid",
        AUTOMATON_DATABASE_URL=f"sqlite:///{tmp_path / 'worker.db'}",
    )

    runtime, _repository, http = build_streaming_runtime(settings, StrategySettings())
    http.close()

    assert isinstance(runtime, StreamingRuntimeCoordinatorService)


def test_composition_uses_one_typed_synchronization_path(tmp_path) -> None:
    settings = AutomatonSettings(
        CORE_URL="http://core.invalid",
        AUTOMATON_DATABASE_URL=f"sqlite:///{tmp_path / 'worker.db'}",
    )

    runtime, _repository, http = build_streaming_runtime(settings, StrategySettings())
    http.close()

    assert isinstance(runtime._synchronization, FactSynchronizationService)
    assert not hasattr(runtime, "_audit_delivery")


def test_streaming_composition_opens_only_supplied_local_sqlite(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'isolated-worker.db'}"
    settings = AutomatonSettings(
        CORE_URL="http://core.invalid",
        AUTOMATON_DATABASE_URL=database_url,
    )
    opened_urls = []
    create_engine = composition.create_worker_engine

    def record_engine(url):
        opened_urls.append(url)
        return create_engine(url)

    monkeypatch.setattr(composition, "create_worker_engine", record_engine)

    _runtime, _repository, http = build_streaming_runtime(settings, StrategySettings())
    http.close()

    assert opened_urls == [database_url]
