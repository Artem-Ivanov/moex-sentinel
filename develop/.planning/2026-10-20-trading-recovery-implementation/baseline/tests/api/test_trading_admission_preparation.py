import asyncio
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from moex_sentinel.api import app as app_module
from moex_sentinel.api.app import _build_readiness_observer, create_app
from moex_sentinel.config import Settings
from moex_sentinel.domain.daily_admission_preparation import (
    DailyAdmissionPreparationReason,
    DailyAdmissionPreparationStatus,
    DailyAdmissionPreparationView,
)
from sentinel_contracts.worker_diagnostics import WorkerObservation


class PreparationStub:
    def __init__(self):
        self.snapshots = []
        self.preparations = []

    async def snapshot(self, broker_id):
        self.snapshots.append(broker_id)
        if broker_id == "missing":
            raise ValueError("not scoped")
        return DailyAdmissionPreparationView(
            moscow_day=None,
            captured_at=None,
            status=DailyAdmissionPreparationStatus.WAITING,
            reason=DailyAdmissionPreparationReason.NOT_OBSERVED,
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )

    async def prepare_once(self):
        self.preparations.append("called")

    async def run(self):
        return None


def test_preparation_get_is_read_only_and_returns_unknown_after_restart(tmp_path, monkeypatch):
    preparation = PreparationStub()
    usecases = SimpleNamespace(daily_admission_preparation=preparation)
    monkeypatch.setattr(app_module, "build_application_usecases", lambda *_args, **_kwargs: usecases)

    with TestClient(
        create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'preparation.db'}")
    ) as client:
        response = client.get("/api/brokers/00000000-0000-4000-8000-000000000001/trading-admission/preparation")
        missing = client.get("/api/brokers/missing/trading-admission/preparation")

    assert response.status_code == 200
    assert response.json()["status"] == "WAITING"
    assert response.json()["reason"] == "NOT_OBSERVED"
    assert response.json()["baseline_id"] is None
    assert missing.status_code == 404
    assert preparation.snapshots == ["00000000-0000-4000-8000-000000000001", "missing"]
    assert preparation.preparations == []


def test_lifespan_awaits_preparation_task_before_closing_executor(tmp_path, monkeypatch):
    events = []
    started = threading.Event()

    class BlockingPreparation(PreparationStub):
        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("task_done")

    class Executor:
        async def run(self, operation):
            return await asyncio.to_thread(operation)

        async def aclose(self):
            events.append("executor_closed")

    class Engine:
        class Dialect:
            name = "sqlite"

        dialect = Dialect()

        def dispose(self):
            events.append("engine_disposed")

    class Gateway:
        async def close(self):
            events.append("gateway_closed")

    settings = Settings(
        _env_file=None,
        application_environment="PROD",
        broker_access_mode="TRADE",
        auth_allowed_origin="https://operator.example.test",
        auth_session_cookie_name="__Host-prod-operator",
        internal_worker_id="worker-prod-1",
        internal_worker_broker_id="00000000-0000-4000-8000-000000000001",
        internal_worker_token="a" * 64,
        daily_admission_preparation_enabled=True,
    )
    monkeypatch.setattr(app_module, "Settings", lambda: settings)
    monkeypatch.setattr(app_module, "SyncExecutor", lambda **_kwargs: Executor())
    pool_options = []

    def create_engine(_url, **kwargs):
        pool_options.append(kwargs)
        return Engine()

    monkeypatch.setattr(app_module, "create_database_engine", create_engine)
    monkeypatch.setattr(app_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: Gateway())
    monkeypatch.setattr(
        app_module,
        "build_application_usecases",
        lambda *_args, **_kwargs: SimpleNamespace(
            daily_admission_preparation=BlockingPreparation(),
            worker_authenticator=None,
        ),
    )

    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'lifespan.db'}")):
        assert started.wait(timeout=2)

    assert events.index("task_done") < events.index("executor_closed")
    assert events.index("task_done") < events.index("engine_disposed")
    assert pool_options == [{"pool_size": 5, "max_overflow": 10, "pool_timeout_seconds": 5.0}]


def test_readiness_alert_observer_uses_account_effective_state_not_preparation_ttl():
    async def preparation_snapshot(_broker_id):
        return DailyAdmissionPreparationView(
            moscow_day=None,
            captured_at=None,
            status=DailyAdmissionPreparationStatus.BLOCKED,
            reason=DailyAdmissionPreparationReason.RECONCILIATION_EXPIRED,
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )

    account = SimpleNamespace(
        admission_status=SimpleNamespace(value="ARMED"),
        effective_can_submit=True,
        recovery_only=False,
    )
    usecases = SimpleNamespace(
        trading_admission=SimpleNamespace(view=lambda _broker_id: account),
        daily_admission_preparation=SimpleNamespace(snapshot=preparation_snapshot),
    )
    worker = SimpleNamespace(diagnostics_snapshot=lambda: (None, WorkerObservation(reason="NOT_OBSERVED")))

    observation = asyncio.run(_build_readiness_observer(usecases, worker, "broker")())

    assert observation.account_status == "ARMED"
    assert observation.account_effective_can_submit is True
    assert observation.preparation_status == "PENDING"
    assert observation.preparation_reason == "ARM_REQUIRED"
