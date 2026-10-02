import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.trading import AutomationState
from tests.trading_automaton.adapters.test_tinvest_broker_session import FakeClient
from tests.trading_automaton.command_factory import command
from tests.trading_automaton.services.test_fact_synchronization import row
from tests.trading_automaton.services.test_streaming_runtime_coordinator_service import bootstrap_command
from tests.trading_automaton.test_streaming_composition import Session, repository
from trading_automaton import composition
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession
from trading_automaton.composition import build_broker_runtime, build_streaming_runtime
from trading_automaton.config import AutomatonSettings, StrategySettings
from trading_automaton.runtime.streaming_coordinator import StreamingRuntimeCoordinator
from trading_automaton.services.batch_runtime import BatchTradingRuntimeService
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.services.order_dispatch import OrderDispatchService
from trading_automaton.storage.database import create_worker_engine, initialize_worker_schema
from trading_automaton.storage.models import FactOutboxModel, LocalIntentModel, WorkerRunModel

SANDBOX = "sandbox-invest-public-api.tbank.ru:443"
PROD = "invest-public-api.tbank.ru:443"


def test_worker_access_mode_is_explicit_and_strategy_disabled(monkeypatch):
    monkeypatch.delenv("BROKER_ACCESS_MODE", raising=False)
    with pytest.raises(ValidationError):
        AutomatonSettings()
    assert StrategySettings().enabled is False
    with pytest.raises(ValidationError):
        AutomatonSettings(BROKER_ACCESS_MODE="unsafe")


def test_read_only_session_blocks_before_accessing_sdk():
    session = BrokerSdkSession("synthetic", PROD, environment="PROD", access_mode="READ_ONLY", account_id="account")
    with pytest.raises(ValueError, match="READ_ONLY"):
        asyncio.run(session.dispatch_limit_order(SimpleNamespace(account_id="account")))


@pytest.mark.parametrize("target", ["sandbox-target", "evil.example:443", PROD])
def test_test_session_rejects_wrong_target(target):
    with pytest.raises(ValueError, match="target"):
        BrokerSdkSession("synthetic", target, environment="TEST", access_mode="TRADE")


def test_prod_trade_session_rejected():
    with pytest.raises(ValueError, match="PROD TRADE"):
        BrokerSdkSession("synthetic", PROD, environment="PROD", access_mode="TRADE", account_id="account")


def test_read_only_dispatch_does_not_update_intent():
    class Repository:
        def update_intent(self, *args, **kwargs):
            raise AssertionError("intent mutated")

    service = OrderDispatchService(Repository(), object(), now=lambda: datetime.now(UTC), access_mode="READ_ONLY")

    async def scenario():
        await service.dispatch(SimpleNamespace(account_id="account"), asyncio.get_running_loop().create_future())

    with pytest.raises(ValueError, match="READ_ONLY"):
        asyncio.run(scenario())


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_read_only_batch_does_not_persist_intent(side):
    class Repository:
        def save_decision_batch(self, *args, **kwargs):
            raise AssertionError("intent persisted")

    service = BatchTradingRuntimeService(
        Repository(), object(), object(), now=lambda: datetime.now(UTC), access_mode="READ_ONLY"
    )
    with pytest.raises(ValueError, match="READ_ONLY"):
        asyncio.run(
            service.run_batch((SimpleNamespace(intent=SimpleNamespace(side=side)),), (), snapshot_at=datetime.now(UTC))
        )


@pytest.mark.parametrize("clean_shutdown", [False, True])
@pytest.mark.parametrize(
    "state", ["CREATED", "DISPATCH_PENDING", "SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED", "UNCERTAIN", "FILLED"]
)
def test_startup_rejects_orphan_sell_intent_without_deleting_history(tmp_path, state, clean_shutdown):
    url = f"sqlite:///{tmp_path / 'isolated.db'}"
    engine = create_worker_engine(url)
    initialize_worker_schema(engine)
    factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with factory.begin() as db:
        db.add(WorkerRunModel(worker_id="worker", clean_shutdown=clean_shutdown))
        db.add(
            LocalIntentModel(
                idempotency_key="pending",
                automation_id="orphan",
                kind="SELL",
                side="SELL",
                state=state,
                quantity_lots=1,
                limit_price=100,
                strategy_snapshot={},
                created_at=now,
                updated_at=now,
            )
        )
    settings = AutomatonSettings(BROKER_ACCESS_MODE="READ_ONLY", AUTOMATON_DATABASE_URL=url)
    with pytest.raises(ValueError, match="READ_ONLY.*active intent"):
        build_streaming_runtime(settings, StrategySettings())
    with factory() as db:
        assert db.get(LocalIntentModel, "pending").state == state


def test_composition_rejects_mode_mismatch_before_session_factory():
    connection = BrokerConnection("broker", "TINVEST_SANDBOX", SANDBOX, "synthetic", True, access_mode="TRADE")

    async def scenario():
        await build_broker_runtime(
            connection,
            object(),
            strategy_settings=StrategySettings(),
            now=lambda: datetime.now(UTC),
            worker_access_mode="READ_ONLY",
            session_factory=lambda _: pytest.fail("SDK factory reached"),
        )

    with pytest.raises(ValueError, match="access mode mismatch"):
        asyncio.run(scenario())


def test_composition_rejects_read_only_strategy_enabled_before_sdk():
    connection = BrokerConnection("broker", "TINVEST_SANDBOX", SANDBOX, "synthetic", True)

    async def scenario():
        await build_broker_runtime(
            connection,
            object(),
            strategy_settings=StrategySettings(enabled=True),
            now=lambda: datetime.now(UTC),
            worker_access_mode="READ_ONLY",
            session_factory=lambda _: pytest.fail("SDK factory reached"),
        )

    with pytest.raises(ValueError, match="READ_ONLY.*strategy"):
        asyncio.run(scenario())


def test_prod_reads_operations_service_and_subtracts_blocked_cash():
    client = FakeClient()

    def money(units):
        return SimpleNamespace(units=units, nano=0, currency="rub")

    async def portfolio(**kwargs):
        assert kwargs["account_id"] == "account"
        return SimpleNamespace(positions=[])

    async def positions(**kwargs):
        return SimpleNamespace(money=[money(100)], blocked=[money(30)], securities=[])

    client.services.operations.get_portfolio = portfolio
    client.services.operations.get_positions = positions

    async def scenario():
        session = BrokerSdkSession(
            "synthetic",
            PROD,
            environment="PROD",
            access_mode="READ_ONLY",
            account_id="account",
            client_factory=lambda *args, **kwargs: client,
        )
        await session.start()
        assert await session.get_positions("account") == ()
        assert await session.get_free_cash("account", "rub") == Decimal(70)
        with pytest.raises(ValueError, match="account mismatch"):
            await session.get_free_cash("wrong", "rub")
        await session.close()

    asyncio.run(scenario())
    assert client.sandbox.portfolio_calls == 0


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize(
    ("units", "nano", "currency"),
    [(1, 1_000_000_000, "rub"), (1, -1, "rub"), (1, 0, ""), (1, 0, "123"), (-1, 0, "rub")],
)
def test_prod_rejects_invalid_money_before_caching(units, nano, currency, blocked):
    client = FakeClient()

    async def positions(**kwargs):
        return SimpleNamespace(
            money=[] if blocked else [SimpleNamespace(units=units, nano=nano, currency=currency)],
            blocked=[SimpleNamespace(units=units, nano=nano, currency=currency)] if blocked else [],
        )

    client.services.operations.get_positions = positions

    async def scenario():
        session = BrokerSdkSession(
            "synthetic",
            PROD,
            environment="PROD",
            access_mode="READ_ONLY",
            account_id="account",
            client_factory=lambda *args, **kwargs: client,
        )
        await session.start()
        with pytest.raises(ValueError, match="money"):
            await session.get_free_cash("account", "rub")
        await session.close()

    asyncio.run(scenario())


def test_coordinator_rejects_wrong_account_before_sdk_builder():
    class Core:
        def broker_connection(self, broker_id):
            return BrokerConnection(broker_id, "TINVEST_PROD", PROD, "synthetic", False, account_id="selected")

    async def builder(_):
        pytest.fail("SDK builder reached")

    runtime = StreamingRuntimeCoordinator(object(), Core(), builder, worker_id="worker", now=lambda: datetime.now(UTC))
    with pytest.raises(ValueError, match="account mismatch"):
        asyncio.run(runtime._replace_broker_commands({"broker": [bootstrap_command()]}))


@pytest.mark.parametrize(
    ("quantity", "blocked", "blocked_lots", "exchange_blocked"),
    [(-1, False, 0, False), (0.5, False, 0, False), (2, True, 0, False), (2, False, 1, False), (2, False, 0, True)],
)
def test_prod_reads_preserve_fractional_and_short_inventory(quantity, blocked, blocked_lots, exchange_blocked):
    client = FakeClient()

    def quotation(value):
        value = Decimal(str(value))
        return SimpleNamespace(units=int(value), nano=int((value - int(value)) * 1_000_000_000))

    async def portfolio(**kwargs):
        return SimpleNamespace(
            positions=[
                SimpleNamespace(
                    instrument_uid="instrument",
                    quantity_lots=quotation(quantity),
                    blocked=blocked,
                    blocked_lots=quotation(blocked_lots),
                    average_position_price=SimpleNamespace(units=100, nano=0, currency="rub"),
                    current_price=SimpleNamespace(units=101, nano=0, currency="rub"),
                )
            ]
        )

    async def inventory(**kwargs):
        return SimpleNamespace(
            securities=[SimpleNamespace(instrument_uid="instrument", blocked=0, exchange_blocked=exchange_blocked)]
        )

    client.services.operations.get_positions = inventory
    client.services.operations.get_portfolio = portfolio

    async def scenario():
        session = BrokerSdkSession(
            "synthetic",
            PROD,
            environment="PROD",
            access_mode="READ_ONLY",
            account_id="account",
            client_factory=lambda *args, **kwargs: client,
        )
        await session.start()
        positions = await session.get_positions("account")
        assert positions[0].quantity_lots == Decimal(str(quantity))
        assert positions[0].blocked is (blocked or bool(blocked_lots) or exchange_blocked)
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("worker_environment", "core_environment"), [("PROD", "TEST"), ("TEST", "PROD")])
def test_worker_contour_pin_rejects_broker_before_sdk(worker_environment, core_environment):
    connection = BrokerConnection(
        "broker",
        "TINVEST_SANDBOX" if core_environment == "TEST" else "TINVEST_PROD",
        SANDBOX if core_environment == "TEST" else PROD,
        "synthetic",
        core_environment == "TEST",
        account_id="account",
    )

    async def scenario():
        await build_broker_runtime(
            connection,
            object(),
            strategy_settings=StrategySettings(),
            now=lambda: datetime.now(UTC),
            application_environment=worker_environment,
            session_factory=lambda _: pytest.fail("SDK reached"),
        )

    with pytest.raises(ValueError, match="environment mismatch"):
        asyncio.run(scenario())


def test_worker_environment_settings_pin(monkeypatch):
    monkeypatch.delenv("APPLICATION_ENVIRONMENT", raising=False)
    assert AutomatonSettings(BROKER_ACCESS_MODE="READ_ONLY").application_environment == "TEST"
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    assert AutomatonSettings(BROKER_ACCESS_MODE="READ_ONLY").application_environment == "PROD"
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "unsafe")
    with pytest.raises(ValidationError):
        AutomatonSettings(BROKER_ACCESS_MODE="READ_ONLY")


@pytest.mark.parametrize(("worker_environment", "core_environment"), [("PROD", "TEST"), ("TEST", "PROD")])
def test_wrong_core_contour_never_receives_claim_or_facts(worker_environment, core_environment):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method != "GET" or request.url.path != "/internal/runtime":
            pytest.fail("wrong Core write reached")
        return httpx.Response(200, json={"environment": core_environment, "access_mode": "READ_ONLY"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as http:
        client = CoreClient(http, application_environment=worker_environment, access_mode="READ_ONLY")
        with pytest.raises(ValueError, match="environment mismatch"):
            client.claim_commands("worker", 1)
        with pytest.raises(ValueError, match="environment mismatch"):
            client.publish_facts([])
    assert requests == [("GET", "/internal/runtime"), ("GET", "/internal/runtime")]


def test_claim_validation_precedes_any_command_cache_write():
    class Repository:
        def cache_command(self, command):
            pytest.fail("wrong contour cached")

    class Client:
        def claim_commands(self, worker_id, limit):
            return [command()]

    def validate(commands):
        raise ValueError("Worker/Core environment mismatch")

    service = FactSynchronizationService(
        Repository(), Client(), now=lambda: datetime.now(UTC), sleep=lambda _: None, validate_commands=validate
    )
    with pytest.raises(ValueError, match="environment mismatch"):
        service.claim_commands("worker", 1)


def test_matching_prod_worker_can_start_read_only_runtime():
    async def scenario():
        session = Session()
        connection = BrokerConnection("broker", "TINVEST_PROD", PROD, "synthetic", False, account_id="account")
        bundle = await build_broker_runtime(
            connection,
            repository(),
            strategy_settings=StrategySettings(),
            application_environment="PROD",
            now=lambda: datetime.now(UTC),
            session_factory=lambda _: session,
        )
        assert session.started == 1
        await bundle.close()

    asyncio.run(scenario())


def test_prod_worker_rejects_trade_mode_before_runtime():
    with pytest.raises(ValidationError, match="PROD TRADE"):
        AutomatonSettings(APPLICATION_ENVIRONMENT="PROD", BROKER_ACCESS_MODE="TRADE")


@pytest.mark.parametrize("mismatch", [None, "account", "environment", "broker", "missing"])
def test_composed_closed_outbox_uses_disabled_broker_scope_without_credentials(monkeypatch, tmp_path, mismatch):
    pending = row()
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/internal/runtime":
            return httpx.Response(200, json={"environment": "PROD", "access_mode": "READ_ONLY"})
        if request.url.path.endswith("/scope"):
            return httpx.Response(
                200,
                json={
                    "broker_id": pending.user_broker_id if mismatch != "broker" else str(UUID(int=1)),
                    "environment": "PROD" if mismatch != "environment" else "TEST",
                    "account_id": "account" if mismatch != "account" else "wrong",
                },
            )
        if request.url.path.endswith("/connection"):
            pytest.fail("disabled broker credentials requested")
        if request.url.path == "/internal/automation-facts":
            return httpx.Response(200, json={"results": [], "failures": []})
        pytest.fail("Unexpected Core request")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core") as mock_http:
        monkeypatch.setattr(composition, "CoreClient", lambda http, **kwargs: CoreClient(mock_http, **kwargs))
        runtime, repository, http = composition.build_streaming_runtime(
            AutomatonSettings(
                APPLICATION_ENVIRONMENT="PROD",
                BROKER_ACCESS_MODE="READ_ONLY",
                AUTOMATON_DATABASE_URL=f"sqlite:///{tmp_path / 'closed.db'}",
            ),
            StrategySettings(),
        )
        cached = command(state=AutomationState.CLOSED).model_copy(
            update={
                "automation_id": UUID(pending.automation_id),
                "user_broker_id": UUID(pending.user_broker_id),
                "broker_id": UUID(pending.user_broker_id),
            }
        )
        if mismatch != "missing":
            repository.cache_command(cached)
        with repository._factory.begin() as db:
            db.add(FactOutboxModel(**pending.model_dump()))
        synchronization = runtime._iteration._synchronization
        if mismatch is None:
            assert synchronization.flush_outbox() is True
            assert sum(path.endswith("/scope") for _, path in requests) == 1
            assert ("POST", "/internal/automation-facts") in requests
        else:
            with pytest.raises(ValueError, match="mismatch|scope is missing"):
                synchronization.flush_outbox()
            assert not any(method == "POST" for method, _ in requests)
        assert repository.has_pending_fact_outbox(pending.automation_id)
        http.close()
