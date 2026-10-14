import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base as CoreBase
from moex_sentinel.storage.models.trading_admission import TradingAccountAdmissionModel, WorkerRuntimeInstanceModel
from sentinel_contracts.broker_execution import BrokerConnection, BrokerPosition
from tests.trading_automaton.command_factory import command as make_command
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.services.test_broker_tick_preparation_service import bootstrap_market
from tests.trading_automaton.services.test_streaming_runtime_coordinator_service import bootstrap_command
from tests.trading_automaton.storage.worker_storage_helpers import INTENT_ID
from trading_automaton import composition
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.composition import BrokerRuntimeBundle, build_broker_runtime, build_streaming_runtime
from trading_automaton.config import AutomatonSettings, StrategySettings
from trading_automaton.domain.dtos import CommissionQuote
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.runtime.streaming_coordinator import StreamingRuntimeCoordinator
from trading_automaton.services.fact_synchronization import FactSynchronizationService
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
            BrokerConnection(
                "broker", "TINVEST_SANDBOX", "sandbox-invest-public-api.tbank.ru:443", "synthetic-token", True
            ),
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
            BrokerConnection(
                str(value.broker_id),
                "TINVEST_SANDBOX",
                "sandbox-invest-public-api.tbank.ru:443",
                "synthetic-token",
                True,
            ),
            repo,
            strategy_settings=StrategySettings(),
            session_factory=lambda _connection: session,
            now=lambda: NOW,
        )
        try:
            await bundle.runtime._iteration._preparation.prepare((value,), bootstrap_market())
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

    assert isinstance(runtime, StreamingRuntimeCoordinator)


def test_composition_uses_one_typed_synchronization_path(tmp_path) -> None:
    settings = AutomatonSettings(
        CORE_URL="http://core.invalid",
        AUTOMATON_DATABASE_URL=f"sqlite:///{tmp_path / 'worker.db'}",
    )

    runtime, _repository, http = build_streaming_runtime(settings, StrategySettings())
    http.close()

    assert isinstance(runtime._iteration._synchronization, FactSynchronizationService)
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


def test_streaming_composition_passes_prod_environment_to_retained_intent_gate(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'prod-retained-intent.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    repo = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    value = make_command()
    repo.cache_command(value)
    repo.save_decision_batch((decision_item(value),), occurred_at=NOW)
    repo.update_intent(INTENT_ID, state="CANCELLED", occurred_at=NOW + timedelta(seconds=1))
    engine.dispose()

    settings = AutomatonSettings(
        APPLICATION_ENVIRONMENT="PROD",
        BROKER_ACCESS_MODE="READ_ONLY",
        AUTOMATON_WORKER_ID="worker-prod",
        INTERNAL_WORKER_ID="worker-prod",
        INTERNAL_WORKER_BROKER_ID=str(value.broker_id),
        INTERNAL_WORKER_TOKEN="a" * 64,
        CORE_URL="http://core.invalid",
        AUTOMATON_DATABASE_URL=database_url,
    )

    with pytest.raises(ValueError, match="READ_ONLY cannot start with active intents"):
        build_streaming_runtime(settings, StrategySettings())


def test_pending_worker_waits_for_actual_core_transfer_then_retries_same_runtime(monkeypatch, tmp_path) -> None:
    broker_id = UUID("00000000-0000-4000-8000-000000000021")
    incumbent_id = UUID("00000000-0000-4000-8000-000000000022")
    worker_token = "a" * 64
    incumbent_secret = "b" * 64
    database_url = f"sqlite:///{tmp_path / 'pending-transfer.db'}"
    for name, value in (
        ("APPLICATION_ENVIRONMENT", "PROD"),
        ("BROKER_ACCESS_MODE", "READ_ONLY"),
        ("AUTH_ALLOWED_ORIGIN", "https://operator.example.test"),
        ("AUTH_SESSION_COOKIE_NAME", "__Host-prod-operator"),
        ("INTERNAL_WORKER_ID", "worker-prod"),
        ("INTERNAL_WORKER_BROKER_ID", str(broker_id)),
        ("INTERNAL_WORKER_TOKEN", worker_token),
    ):
        monkeypatch.setenv(name, value)
    engine = create_database_engine(database_url)
    CoreBase.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO user_brokers "
            "(id, api_slug, display_name, environment, fqdn, settings, external_account_id, state, "
            "created_at, updated_at) "
            "VALUES (?, 't_invest', 'Synthetic', 'PROD', 'invest-public-api.tbank.ru:443', ?, 'account-1', "
            "'ACTIVE', ?, ?)",
            (str(broker_id), '{"token":"synthetic-token"}', NOW.isoformat(), NOW.isoformat()),
        )
    engine.dispose()

    app = create_app(test_auth_bypass=True, database_url=database_url)
    with TestClient(app) as core_http:
        owner_headers = {
            "Authorization": f"Bearer {worker_token}",
            "X-Runtime-Instance-ID": str(incumbent_id),
            "X-Runtime-Secret": incumbent_secret,
        }
        owner = core_http.post(
            "/internal/automaton/runtime-instances",
            json={
                "worker_id": "worker-prod",
                "runtime_instance_id": str(incumbent_id),
                "broker_id": str(broker_id),
                "application_environment": "PROD",
                "access_mode": "READ_ONLY",
                "strategy_enabled": False,
            },
            headers=owner_headers,
        )
        assert owner.status_code == 200
        assert owner.json()["registration_status"] == "OWNER"

        worker_requests = []

        def forward(request: httpx.Request) -> httpx.Response:
            worker_requests.append(request)
            response = core_http.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.headers.get("content-type", "application/json")},
            )

        with httpx.Client(transport=httpx.MockTransport(forward), base_url="http://core") as worker_http:
            worker = CoreClient(
                worker_http,
                application_environment="PROD",
                access_mode="READ_ONLY",
                worker_id="worker-prod",
                broker_id=broker_id,
                worker_token=worker_token,
            )
            worker_instance_id = worker.runtime_instance_id
            with pytest.raises(CoreOperationError) as pending:
                worker.broker_connection(str(broker_id))
            pending_error = pending.value
            pending_paths = [request.url.path for request in worker_requests]

            sessions = sessionmaker(app.state.database_engine, expire_on_commit=False)
            with sessions.begin() as session:
                incumbent = session.get(WorkerRuntimeInstanceModel, str(incumbent_id))
                candidate = session.get(WorkerRuntimeInstanceModel, str(worker_instance_id))
                admission = session.get(TradingAccountAdmissionModel, str(broker_id))
                incumbent.registration_status = "RETIRED"
                incumbent.retired_at = NOW
                candidate.registration_status = "OWNER"
                candidate.generation = 2
                candidate.lease_expires_at = NOW + timedelta(seconds=30)
                admission.owner_runtime_instance_id = str(worker_instance_id)
                admission.owner_generation = 2
                admission.lease_expires_at = candidate.lease_expires_at

            connection = worker.broker_connection(str(broker_id))
            all_paths = [request.url.path for request in worker_requests]
            registration_requests = [
                request for request in worker_requests if request.url.path.endswith("/runtime-instances")
            ]

    assert isinstance(pending_error, CoreOperationError)
    assert pending_error.retryable is True
    assert pending_paths == ["/internal/automaton/runtime-instances"]
    assert len(registration_requests) == 2
    assert all(
        json.loads(request.content)["runtime_instance_id"] == str(worker_instance_id)
        for request in registration_requests
    )
    assert registration_requests[0].headers["X-Runtime-Secret"] == registration_requests[1].headers["X-Runtime-Secret"]
    assert connection.account_id == "account-1"
    assert all_paths[-1].endswith("/connection")


def test_composed_empty_control_iteration_delivers_actual_worker_diagnostics_to_core(tmp_path, monkeypatch):
    application = create_app(
        test_auth_bypass=True,
        database_url=f"sqlite:///{tmp_path/'core-diagnostics.db'}",
        schema_checker=lambda _engine: True,
    )
    with TestClient(application) as client:
        CoreBase.metadata.create_all(application.state.database_engine)

        def forward(request):
            response = client.request(
                request.method, request.url.path, content=request.content, headers={"content-type": "application/json"}
            )
            return httpx.Response(response.status_code, content=response.content)

        with httpx.Client(base_url="http://core", transport=httpx.MockTransport(forward)) as forwarding:
            core = CoreClient(forwarding, application_environment="TEST", access_mode="READ_ONLY")
            monkeypatch.setattr(composition, "CoreClient", lambda *_args, **_kwargs: core)
            runtime, repository, http = build_streaming_runtime(
                AutomatonSettings(
                    CORE_URL="http://core.invalid",
                    AUTOMATON_DATABASE_URL=f"sqlite:///{tmp_path/'worker-diagnostics.db'}",
                ),
                StrategySettings(),
            )

            async def scenario():
                try:
                    await runtime.run_iteration()
                finally:
                    await runtime.close()

            try:
                asyncio.run(scenario())
                snapshot = client.get("/api/diagnostics/status").json()
                assert snapshot["worker"]["completed_iterations"] == 1
                assert snapshot["worker"]["reason"] == "CONTROL_PROGRESS"
                assert snapshot["worker"]["outbox"]["reason"] == "CLEAR"
                assert snapshot["status"] == "UNKNOWN"
                assert repository.outbox_diagnostics().pending_count == 0
            finally:
                http.close()


@pytest.mark.parametrize("failure", ["read", "restore", "cancel"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_builder_rolls_back_started_session_preserving_primary_error(monkeypatch, failure, close_fails):
    primary = asyncio.CancelledError() if failure == "cancel" else RuntimeError("reservation failure")

    class FailingSession(Session):
        async def close(self):
            await super().close()
            if close_fails:
                raise RuntimeError("close failure")

    repo = repository()
    if failure == "read":

        def fail_read(*args):
            raise primary

        monkeypatch.setattr(repo, "list_active_buy_intent_reservations", fail_read)
    else:

        async def fail_restore(self, values):
            raise primary

        monkeypatch.setattr(composition.AccountCashReservationService, "restore", fail_restore)

    async def scenario():
        session = FailingSession()
        with pytest.raises(type(primary)) as caught:
            await build_broker_runtime(
                BrokerConnection(
                    "broker", "TINVEST_SANDBOX", "sandbox-invest-public-api.tbank.ru:443", "synthetic-token", True
                ),
                repo,
                strategy_settings=StrategySettings(),
                session_factory=lambda connection: session,
                now=lambda: NOW,
            )
        assert caught.value is primary
        assert session.started == session.closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("injected", [False, True])
def test_builder_closes_only_internally_owned_analytics_on_late_failure(monkeypatch, injected):
    primary = RuntimeError("late construction failure")
    clients = []

    class Analytics:
        def __init__(self, http=None):
            self.http = http
            self.closed = 0
            clients.append(self)

        async def close(self):
            self.closed += 1
            if self.http is not None:
                await self.http.aclose()

    def fail_runtime(*args, **kwargs):
        raise primary

    monkeypatch.setattr(composition, "AnalyticsClient", Analytics)
    monkeypatch.setattr(composition, "AnalyticsBrokerRuntime", fail_runtime)

    async def scenario():
        session = Session()
        client = Analytics() if injected else None
        with pytest.raises(RuntimeError) as caught:
            await build_broker_runtime(
                BrokerConnection(
                    "broker", "TINVEST_SANDBOX", "sandbox-invest-public-api.tbank.ru:443", "synthetic-token", True
                ),
                repository(),
                strategy_settings=StrategySettings(),
                session_factory=lambda connection: session,
                now=lambda: NOW,
                analytics_client=client,
            )
        assert caught.value is primary
        assert session.started == session.closed == 1
        assert clients[0].closed == (0 if injected else 1)

    asyncio.run(scenario())


def test_builder_does_not_close_session_that_failed_to_start():
    primary = RuntimeError("session start failed")

    class FailingSession(Session):
        async def start(self):
            await super().start()
            raise primary

    async def scenario():
        session = FailingSession()
        with pytest.raises(RuntimeError) as caught:
            await build_broker_runtime(
                BrokerConnection(
                    "broker", "TINVEST_SANDBOX", "sandbox-invest-public-api.tbank.ru:443", "synthetic-token", True
                ),
                repository(),
                strategy_settings=StrategySettings(),
                session_factory=lambda connection: session,
                now=lambda: NOW,
            )
        assert caught.value is primary
        assert session.started == 1
        assert session.closed == 0

    asyncio.run(scenario())
