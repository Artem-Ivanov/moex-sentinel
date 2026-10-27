"""Full PROD decision path through Analytics, Worker durability and Core PostgreSQL."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from t_tech.invest.schemas import Quotation

from market_analytics.app import create_app as create_analytics_app
from market_analytics.config import AnalyticsSettings
from market_analytics.market_source import HttpMarketSource
from moex_sentinel.adapters.tinvest import admission_risk as admission_risk_module
from moex_sentinel.adapters.tinvest import market_stream_source as market_stream_module
from moex_sentinel.api import app as core_app_module
from moex_sentinel.composition import TInvestMarketStreamSource as RealMarketStreamSource
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import BrokerOrderModel
from moex_sentinel.storage.models.order_facts import TradeExecutionModel
from sentinel_contracts.analytics import AdaptiveThresholds, AnalyticsSnapshotRequest
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_admission import (
    AdmissionLimits,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    ReconciliationRequest,
    RuntimeRegistrationRequest,
    UpdateLimitsRequest,
)
from sentinel_contracts.trading_facts import FactEnvelope, FactKind
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
from tests.trading_automaton.storage.worker_storage_helpers import baseline_command
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.composition import build_broker_runtime
from trading_automaton.config import StrategySettings
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base as WorkerBase
from trading_automaton.storage.models import FactOutboxModel, LocalIntentModel, TradeDecisionModel
from trading_automaton.storage.repository import LocalAutomationRepository

pytestmark = pytest.mark.postgresql

BROKER_ID = UUID("00000000-0000-4000-8000-000000000911")
WORKER_ID = "worker-prod-decision-pipeline"
WORKER_TOKEN = "c" * 64
ACCOUNT_ID = "decision-pipeline-account"
INSTRUMENT_UID = "decision-pipeline-uid"
INSTRUMENT_ID = "00000000-0000-4000-8000-000000000912"
AUTOMATION_ID = "00000000-0000-4000-8000-000000000913"
BROKER_TOKEN = "synthetic-prod-token"
TARGET = "invest-public-api.tbank.ru:443"
ACTUAL_FILL_LOTS = 1
ACTUAL_FILL_PRICE = Decimal("99.75")
ACTUAL_FILL_COMMISSION = Decimal("1.25")
FACT_ENVELOPE_ADAPTER = TypeAdapter(FactEnvelope)


def _now() -> datetime:
    current = datetime.now(UTC)
    return current.replace(microsecond=current.microsecond // 1000 * 1000)


def _quote(value: Decimal) -> Quotation:
    units = int(value)
    nano = int((value - units) * Decimal("1000000000"))
    return Quotation(units=units, nano=nano)


def _money(value: Decimal, currency: str = "RUB"):
    quote = _quote(value)
    return SimpleNamespace(units=quote.units, nano=quote.nano, currency=currency)


class _StreamManager:
    def __init__(self, instrument_id: str):
        self.instrument_id = instrument_id
        self.stopped = False
        self.order_book = self.last_price = self.info = self.candles = self

    def subscribe(self, _instruments):
        return None

    def unsubscribe(self, _instruments):
        return None

    def waiting_close(self, _enabled=True):
        return self

    def stop(self):
        self.stopped = True

    def __aiter__(self):
        return self.events()

    async def events(self):
        while not self.stopped:
            now = _now()
            yield SimpleNamespace(
                orderbook=SimpleNamespace(
                    instrument_uid=self.instrument_id,
                    bids=[SimpleNamespace(price=_quote(Decimal("100")), quantity=5)],
                    asks=[SimpleNamespace(price=_quote(Decimal("101")), quantity=5)],
                    time=now,
                    is_consistent=True,
                )
            )
            yield SimpleNamespace(
                trading_status=SimpleNamespace(
                    instrument_uid=self.instrument_id,
                    trading_status="SECURITY_TRADING_STATUS_NORMAL",
                    limit_order_available_flag=True,
                    time=now,
                )
            )
            await asyncio.sleep(0.01)


class _FakeSdk:
    """Synthetic provider responses at the SDK boundary used by each real adapter."""

    def __init__(self, token: str, owner):
        self.token = token
        self.owner = owner
        self.orders = SimpleNamespace(
            get_orders=self.get_orders,
            get_order_price=self.get_order_price,
            post_order=self.post_order,
            get_order_state=self.get_order_state,
        )
        self.operations = SimpleNamespace(
            get_portfolio=self.get_portfolio,
            get_positions=self.get_positions,
            get_operations_by_cursor=self.get_operations_by_cursor,
        )
        self.stop_orders = SimpleNamespace(get_stop_orders=self.get_stop_orders)
        self.instruments = SimpleNamespace(get_instrument_by=self.get_instrument_by)
        self.market_data = SimpleNamespace(
            get_order_book=self.get_order_book,
            get_candles=self.get_candles,
            get_trading_status=self.get_trading_status,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def create_market_data_stream(self):
        self.owner.sdk_calls.append("create_market_data_stream")
        return _StreamManager(INSTRUMENT_UID)

    async def get_portfolio(self, *, account_id):
        return SimpleNamespace(
            account_id=account_id,
            total_amount_portfolio=_money(Decimal("100000")),
            positions=(),
        )

    async def get_positions(self, *, account_id):
        return SimpleNamespace(
            account_id=account_id,
            money=[_money(Decimal("100000"))],
            blocked=[_money(Decimal(0))],
            securities=[],
            limits_loading_in_progress=False,
            futures=[],
            options=[],
        )

    async def get_orders(self, *, account_id):
        return SimpleNamespace(orders=[])

    async def get_stop_orders(self, *, account_id, status):
        return SimpleNamespace(stop_orders=[])

    async def get_operations_by_cursor(self, request):
        return SimpleNamespace(items=[], has_next=False, next_cursor="")

    async def get_instrument_by(self, *, id_type, id):
        return SimpleNamespace(
            instrument=SimpleNamespace(
                uid=id,
                currency="RUB",
                lot=10,
                min_price_increment=_quote(Decimal("0.01")),
                instrument_kind="INSTRUMENT_TYPE_SHARE",
                api_trade_available_flag=True,
            )
        )

    async def get_order_book(self, *, instrument_id, depth):
        now = _now()
        return SimpleNamespace(
            instrument_uid=instrument_id,
            orderbook_ts=now,
            bids=[SimpleNamespace(price=_quote(Decimal("100")), quantity=5)],
            asks=[SimpleNamespace(price=_quote(Decimal("101")), quantity=5)],
        )

    async def get_candles(self, *, instrument_id, from_, to, interval):
        self.owner.sdk_calls.append("get_candles")
        now = _now()
        return SimpleNamespace(
            candles=[
                SimpleNamespace(
                    open=_quote(Decimal(100 + i)),
                    high=_quote(Decimal(102 + i)),
                    low=_quote(Decimal(99 + i)),
                    close=_quote(Decimal(101 + i)),
                    volume=10,
                    time=now - timedelta(minutes=20 - i),
                    is_complete=True,
                    last_trade_ts=now,
                )
                for i in range(20)
            ]
        )

    async def get_trading_status(self, *, instrument_id):
        self.owner.sdk_calls.append("get_trading_status")
        return SimpleNamespace(api_trade_available_flag=True)

    async def get_order_price(self, request):
        requested = getattr(request, "quantity", 1)
        return SimpleNamespace(
            lots_requested=requested,
            initial_order_amount=_money(Decimal("1010")),
            executed_commission=_money(Decimal("1.25")),
            service_commission=_quote(Decimal("0.75")),
            deal_commission=_quote(Decimal("0.50")),
        )

    async def post_order(self, **request):
        self.owner.posts.append(request)
        self.owner.request_id = request["order_id"]
        now = _now()
        return SimpleNamespace(
            order_id="synthetic-broker-order-1",
            order_request_id=request["order_id"],
            execution_report_status="EXECUTION_REPORT_STATUS_NEW",
            lots_requested=request["quantity"],
            lots_executed=0,
            initial_order_price=_money(Decimal("1010")),
            total_order_amount=_quote(Decimal(0)),
            initial_commission=_quote(ACTUAL_FILL_COMMISSION),
            executed_commission=_quote(Decimal(0)),
            executed_order_price=_quote(Decimal(0)),
            order_date=now,
        )

    async def get_order_state(self, *, account_id, order_id, **_kwargs):
        now = _now()
        return SimpleNamespace(
            order_id="synthetic-broker-order-1",
            order_request_id=self.owner.request_id,
            execution_report_status="EXECUTION_REPORT_STATUS_FILL",
            direction="ORDER_DIRECTION_BUY",
            instrument_uid=INSTRUMENT_UID,
            lots_requested=ACTUAL_FILL_LOTS,
            lots_executed=ACTUAL_FILL_LOTS,
            initial_order_price=_money(Decimal("1010")),
            total_order_amount=_quote(ACTUAL_FILL_PRICE * Decimal(10) * ACTUAL_FILL_LOTS),
            average_position_price=_quote(ACTUAL_FILL_PRICE),
            executed_commission=_quote(ACTUAL_FILL_COMMISSION),
            initial_commission=_quote(ACTUAL_FILL_COMMISSION),
            executed_at=now,
            order_date=now,
            account_id=account_id,
        )


class _FakeSdkClient:
    def __init__(self, token: str, *, target: str, owner):
        self.services = _FakeSdk(token, owner)

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *_args):
        return None


class _SdkBoundary:
    def __init__(self):
        self.posts = []
        self.request_id = None
        self.sdk_calls = []

    def client(self, token: str, *, target: str):
        self.sdk_calls.append(("client", token, target))
        return _FakeSdkClient(token, target=target, owner=self)


def _core_transport(core: TestClient, paths=None):
    def send(request: httpx.Request) -> httpx.Response:
        response = core.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            headers=dict(request.headers),
            content=request.content,
        )
        if paths is not None:
            body = response.text if request.url.path == "/internal/automation-facts" else response.text[:500]
            paths.append((request.url.path, response.status_code, body))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)

    return httpx.MockTransport(send)


@pytest.fixture
def prod_test_database(isolated_postgresql_database_url):
    assert isolated_postgresql_database_url.host == "127.0.0.1"
    assert 1 <= isolated_postgresql_database_url.port <= 65535
    assert isolated_postgresql_database_url.database == "sentinel_prod_completeness_test"
    return isolated_postgresql_database_url


def test_prod_http_decision_is_durable_and_acknowledged_with_actual_fill(prod_test_database, tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
        monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
        monkeypatch.setenv("AUTH_USERNAME", "synthetic-test-operator")
        monkeypatch.setenv("AUTH_PASSWORD_HASH", f"scrypt:16384:8:5:{'0' * 32}:{'0' * 64}")
        monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
        monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-pipeline-test-session")
        monkeypatch.setenv("INTERNAL_WORKER_ID", WORKER_ID)
        monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(BROKER_ID))
        monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)

        boundary = _SdkBoundary()
        monkeypatch.setattr(admission_risk_module, "AsyncClient", boundary.client)
        monkeypatch.setattr(market_stream_module, "AsyncClient", boundary.client)
        core_composition = __import__("moex_sentinel.composition", fromlist=["TInvestMarketStreamSource"])
        monkeypatch.setattr(
            core_composition,
            "TInvestMarketStreamSource",
            lambda token, target: RealMarketStreamSource(token, target, client_factory=boundary.client),
        )
        worker_sdk_module = __import__("trading_automaton.adapters.tinvest_broker_session", fromlist=["AsyncClient"])
        monkeypatch.setattr(worker_sdk_module, "AsyncClient", boundary.client)

        engine = create_database_engine(prod_test_database)
        factory = sessionmaker(engine, expire_on_commit=False)
        worker_engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker-prod-pipeline.db'}")
        WorkerBase.metadata.create_all(worker_engine)
        worker_factory = sessionmaker(worker_engine, expire_on_commit=False)
        repository = LocalAutomationRepository(
            worker_factory,
            fact_writer=FactOutboxWriter(clock=_now),
            environment="PROD",
        )

        command = baseline_command().model_copy(
            update={
                "user_broker_id": BROKER_ID,
                "broker_id": BROKER_ID,
                "account_id": ACCOUNT_ID,
                "automation_id": UUID(AUTOMATION_ID),
                "instrument_id": UUID(INSTRUMENT_ID),
                "external_instrument_id": INSTRUMENT_UID,
                "lot_size": 10,
                "state": AutomationState.IN_WORK,
            }
        )
        now = _now()
        with factory.begin() as session:
            broker = user_broker_model(str(BROKER_ID), ACCOUNT_ID)
            broker.environment = "PROD"
            broker.fqdn = TARGET
            broker.settings = {"token": BROKER_TOKEN}
            session.add(broker)
            session.flush()
            instrument = instrument_model(INSTRUMENT_ID, str(BROKER_ID))
            instrument.external_instrument_id = INSTRUMENT_UID
            instrument.ticker = "PIPE"
            session.add(instrument)
            session.flush()
            session.add(
                automation_model(
                    AUTOMATION_ID,
                    user_broker_id=str(BROKER_ID),
                    instrument_id=INSTRUMENT_ID,
                    state="IN_WORK",
                )
            )

        limits = AdmissionLimits(
            currency="RUB",
            max_order_notional=Decimal("10000"),
            max_exposure=Decimal("100000"),
            max_outstanding_orders=4,
            max_daily_loss=Decimal("1000"),
        )
        readonly_app = core_app_module.create_app(database_url=prod_test_database)
        with TestClient(readonly_app) as readonly_core:
            old_http = httpx.Client(transport=_core_transport(readonly_core), base_url="http://core.test")
            old_worker = CoreClient(
                old_http,
                application_environment="PROD",
                access_mode="READ_ONLY",
                worker_id=WORKER_ID,
                broker_id=BROKER_ID,
                worker_token=WORKER_TOKEN,
                strategy_enabled=False,
            )
            old_worker.validate_runtime()
            old_registration = old_worker._registration
            assert old_registration is not None
            assert old_registration.registration_status.value == "OWNER"
            old_http.close()

        monkeypatch.setenv("BROKER_ACCESS_MODE", "TRADE")
        app = core_app_module.create_app(database_url=prod_test_database)
        bundle = None
        worker_http = None
        analytics_http = None
        analytics_worker_http = None
        try:
            with TestClient(app) as core:
                core_http_paths = []
                admission = core.app.state.usecases.trading_admission
                worker_http = httpx.Client(
                    transport=_core_transport(core, core_http_paths), base_url="http://core.test"
                )
                core_client = CoreClient(
                    worker_http,
                    application_environment="PROD",
                    access_mode="TRADE",
                    worker_id=WORKER_ID,
                    broker_id=BROKER_ID,
                    worker_token=WORKER_TOKEN,
                    strategy_enabled=True,
                )
                candidate = core_client.register_runtime(
                    RuntimeRegistrationRequest(
                        worker_id=WORKER_ID,
                        runtime_instance_id=core_client.runtime_instance_id,
                        broker_id=BROKER_ID,
                        application_environment="PROD",
                        access_mode="TRADE",
                        strategy_enabled=True,
                    )
                )
                assert candidate.registration_status.value == "PENDING"
                prior = admission.view(str(BROKER_ID))
                transferred = admission.owner_stopped(
                    str(BROKER_ID),
                    OwnerStoppedRequest(
                        expected_revision=prior.revision,
                        old_runtime_instance_id=old_registration.runtime_instance_id,
                        old_generation=old_registration.generation,
                        candidate_runtime_instance_id=core_client.runtime_instance_id,
                        stopped_at=_now(),
                        auto_restart_disabled=True,
                        evidence_ref="test://synthetic-readonly-owner-stopped",
                    ),
                    actor="synthetic-test-operator",
                )
                assert transferred.owner_runtime_instance_id == core_client.runtime_instance_id
                core_client.validate_runtime()
                owner = core_client._registration
                assert owner is not None
                assert owner.registration_status.value == "OWNER"

                before_limits = admission.view(str(BROKER_ID))
                admission.update_limits(
                    str(BROKER_ID),
                    UpdateLimitsRequest(expected_revision=before_limits.revision, limits=limits),
                    actor="synthetic-test-operator",
                )
                baseline = await admission.capture_baseline(
                    str(BROKER_ID),
                    CaptureBaselineRequest(day=now.astimezone(UTC).date(), accept_policy=True),
                    accepted_by="synthetic-test-operator",
                )

                reconciled = await admission.reconcile(
                    str(BROKER_ID),
                    ReconciliationRequest(
                        expected_revision=admission.view(str(BROKER_ID)).revision, canary_uid=INSTRUMENT_UID
                    ),
                    runtime_instance_id=owner.runtime_instance_id,
                    generation=owner.generation,
                )
                ready = admission.view(str(BROKER_ID))
                armed = admission.arm(
                    str(BROKER_ID),
                    ArmRequest(
                        expected_revision=ready.revision,
                        runtime_instance_id=owner.runtime_instance_id,
                        baseline_id=baseline.baseline_id,
                        reconciliation_id=reconciled.reconciliation_id,
                        limits=limits,
                        configuration_fingerprint=ready.configuration_fingerprint,
                        expires_at=_now() + timedelta(minutes=2),
                    ),
                    actor="synthetic-test-operator",
                )
                assert armed.admission_status.value == "ARMED"

                connection = core_client.broker_connection(str(BROKER_ID))
                assert connection.environment == "PROD"
                assert connection.access_mode == "TRADE"
                analytics_http = httpx.AsyncClient(
                    transport=_core_transport(core, core_http_paths), base_url="http://core.test", timeout=5
                )
                analytics_app = create_analytics_app(
                    HttpMarketSource(analytics_http),
                    settings=AnalyticsSettings(core_url="http://core.test"),
                    now=_now,
                )
                analytics_worker_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=analytics_app), base_url="http://analytics.test"
                )
                analytics_client = AnalyticsClient(analytics_worker_http)
                source_request = AnalyticsSnapshotRequest(
                    source_id=BROKER_ID,
                    instrument_ids=(INSTRUMENT_UID,),
                    fallback=AdaptiveThresholds(
                        averaging_step_percent=Decimal("0.5"),
                        minimum_net_profit_percent=Decimal("0.5"),
                        source="STRATEGY",
                    ),
                )
                for _ in range(50):
                    market_frame = await analytics_client.snapshot(source_request)
                    if market_frame.instruments[0].available and market_frame.instruments[0].candles:
                        break
                    await asyncio.sleep(0.02)
                assert market_frame.instruments[0].available, (market_frame.instruments[0], boundary.sdk_calls)
                assert len(market_frame.instruments[0].candles) >= 20

                bundle = await build_broker_runtime(
                    connection,
                    repository,
                    strategy_settings=StrategySettings(enabled=True),
                    worker_access_mode="TRADE",
                    application_environment="PROD",
                    now=_now,
                    core_client=core_client,
                    analytics_client=analytics_client,
                )
                repository.cache_command(command)
                repository.save_cycle_state(
                    TradingCycleState(str(AUTOMATION_ID), Decimal("90"), None, True, None, _now())
                )
                await bundle.runtime.replace_commands((command,))
                await bundle.runtime.run_once()
                await bundle.tracking.wait_all()
                assert bundle.runtime.broker_runtime_diagnostics().decision_eligibility == "READY"

                with worker_factory() as session:
                    decisions = session.scalars(select(TradeDecisionModel)).all()
                    intents = session.scalars(
                        select(LocalIntentModel).where(LocalIntentModel.automation_id == AUTOMATION_ID)
                    ).all()
                    outbox_rows = session.scalars(
                        select(FactOutboxModel).order_by(FactOutboxModel.sequence_number)
                    ).all()
                    assert len(decisions) == 1, (bundle.runtime.broker_runtime_diagnostics(), core_http_paths)
                assert decisions[0].reason_code == "ENTRY_REVERSAL_CONFIRMED"
                assert len(intents) == 1
                assert intents[0].state == "FILLED"
                assert intents[0].executed_lots == ACTUAL_FILL_LOTS
                assert intents[0].executed_price == ACTUAL_FILL_PRICE
                assert intents[0].executed_commission == ACTUAL_FILL_COMMISSION
                assert len(boundary.posts) == 1
                fact_envelopes = [
                    FACT_ENVELOPE_ADAPTER.validate_python(
                        {
                            "event_id": row.event_id,
                            "user_broker_id": row.user_broker_id,
                            "automation_id": row.automation_id,
                            "sequence_number": row.sequence_number,
                            "expected_revision": row.expected_revision,
                            "fact_kind": row.fact_kind,
                            "payload": row.payload,
                            "safe_message": row.safe_message,
                            "occurred_at": row.occurred_at,
                        }
                    )
                    for row in outbox_rows
                ]
                assert [fact.sequence_number for fact in fact_envelopes] == list(range(1, len(fact_envelopes) + 1))
                assert [fact.fact_kind for fact in fact_envelopes] == [
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.TRADE_DECISION_RECORDED,
                    FactKind.BROKER_ORDER_RECORDED,
                    FactKind.BROKER_ORDER_STATE_CHANGED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.BROKER_ORDER_STATE_CHANGED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.BROKER_ORDER_STATE_CHANGED,
                    FactKind.POSITION_CYCLE_UPDATED,
                    FactKind.TRADE_EXECUTION_RECORDED,
                    FactKind.POSITION_LOT_OPENED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.TRADE_AUDIT_RECORDED,
                    FactKind.TRADE_AUDIT_RECORDED,
                ]
                execution_facts = [
                    fact for fact in fact_envelopes if fact.fact_kind == FactKind.TRADE_EXECUTION_RECORDED
                ]
                assert len(execution_facts) == 1
                assert execution_facts[0].payload.executed_lots == ACTUAL_FILL_LOTS
                assert execution_facts[0].payload.price == ACTUAL_FILL_PRICE
                assert execution_facts[0].payload.broker_commission == ACTUAL_FILL_COMMISSION
                assert repository.ready_fact_outbox(1000, now=_now() + timedelta(minutes=1), deadline_ms=0)
                synchronization = FactSynchronizationService(
                    repository,
                    core_client,
                    now=_now,
                    sleep=lambda _seconds: None,
                    batch_size=1000,
                    deadline_ms=0,
                )
                assert await asyncio.to_thread(synchronization.flush_outbox) is True
                pending_facts = repository.ready_fact_outbox(1000, now=_now() + timedelta(minutes=1), deadline_ms=0)
                fact_responses = [entry for entry in core_http_paths if entry[0] == "/internal/automation-facts"]
                assert pending_facts == []
                assert len(fact_responses) == 1
                assert fact_responses[0][1] == 200
                acknowledgement = json.loads(fact_responses[0][2])
                assert acknowledgement["failures"] == []
                assert len(acknowledgement["results"]) == 1
                assert acknowledgement["results"][0]["accepted_through_sequence"] == fact_envelopes[-1].sequence_number
                outbox_diagnostics = repository.outbox_diagnostics()
                assert outbox_diagnostics.pending_count == 0
                assert outbox_diagnostics.failed_count == 0

                with factory() as session:
                    orders = session.scalars(select(BrokerOrderModel)).all()
                    core_executions = session.scalars(select(TradeExecutionModel)).all()
                assert len(orders) == 1
                assert orders[0].state == "FILLED"
                assert len(core_executions) == 1
                assert core_executions[0].executed_lots == ACTUAL_FILL_LOTS
                assert core_executions[0].price == ACTUAL_FILL_PRICE
                assert core_executions[0].broker_commission == ACTUAL_FILL_COMMISSION
                assert len(boundary.posts) == 1
        finally:
            if bundle is not None:
                await bundle.close()
            if analytics_http is not None:
                await analytics_http.aclose()
            if analytics_worker_http is not None:
                await analytics_worker_http.aclose()
            if worker_http is not None:
                worker_http.close()
            worker_engine.dispose()
            engine.dispose()

    asyncio.run(scenario())
