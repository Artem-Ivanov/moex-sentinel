import asyncio
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from moex_sentinel import composition as composition_module
from moex_sentinel.api import app as app_module
from moex_sentinel.config import Settings
from moex_sentinel.storage.database import create_database_engine as actual_create_database_engine
from sentinel_contracts.runtime_versions import VersionObservation
from sentinel_contracts.worker_diagnostics import WorkerObservation


def test_readiness_observer_reads_expected_work_from_scoped_trading_service(monkeypatch) -> None:
    broker_id = uuid4()
    scopes = []

    class TradingAdmission:
        def expects_trading(self, value):
            scopes.append(value)
            return True

        def view(self, _value):
            return None

    class WorkerVersions:
        def diagnostics_snapshot(self):
            return VersionObservation(reason="NOT_OBSERVED"), WorkerObservation(reason="NOT_OBSERVED")

    async def direct_run(operation):
        return operation()

    async def no_preparation(_broker_id):
        return None

    monkeypatch.setattr(app_module, "run_sync", direct_run)
    observer = app_module._build_readiness_observer(
        SimpleNamespace(
            trading_admission=TradingAdmission(),
            daily_admission_preparation=SimpleNamespace(snapshot=no_preparation),
        ),
        WorkerVersions(),
        broker_id,
    )

    result = asyncio.run(observer())

    assert result.expected_work is True
    assert scopes == [broker_id]


def test_readiness_observer_exposes_only_allowlisted_admission_denials(monkeypatch) -> None:
    broker_id = uuid4()

    class TradingAdmission:
        def expects_trading(self, _value):
            return True

        def view(self, _value):
            return SimpleNamespace(
                admission_status=SimpleNamespace(value="ARMED"),
                effective_can_submit=False,
                effective_denial_reasons=(
                    SimpleNamespace(value="LIMITS_NOT_CONFIGURED"),
                    SimpleNamespace(value="STALE_MARKET"),
                    SimpleNamespace(value="synthetic-secret-reason"),
                ),
                recovery_only=False,
            )

    class WorkerVersions:
        def diagnostics_snapshot(self):
            return VersionObservation(reason="NOT_OBSERVED"), WorkerObservation(reason="NOT_OBSERVED")

    async def direct_run(operation):
        return operation()

    async def no_preparation(_broker_id):
        return None

    monkeypatch.setattr(app_module, "run_sync", direct_run)
    observer = app_module._build_readiness_observer(
        SimpleNamespace(
            trading_admission=TradingAdmission(),
            daily_admission_preparation=SimpleNamespace(snapshot=no_preparation),
        ),
        WorkerVersions(),
        broker_id,
    )

    result = asyncio.run(observer())

    assert result.account_effective_can_submit is False
    assert result.effective_denial_reasons == ("LIMITS_NOT_CONFIGURED",)


def test_http_backend_passes_configured_database_pool_to_engine(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        broker_access_mode="READ_ONLY",
        database_pool_size=7,
        database_max_overflow=3,
        database_pool_timeout_seconds=2.5,
    )
    monkeypatch.setattr(app_module, "Settings", lambda: settings)
    calls = []

    def capture_engine(database_url, **kwargs):
        calls.append(kwargs)
        return actual_create_database_engine(database_url)

    monkeypatch.setattr(app_module, "create_database_engine", capture_engine)
    with TestClient(app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'pool.db'}")):
        pass

    assert calls == [{"pool_size": 7, "max_overflow": 3, "pool_timeout_seconds": 2.5}]


def test_http_backend_passes_analytics_and_alert_timings_to_consumers(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        broker_access_mode="READ_ONLY",
        telegram_alerts_enabled=True,
        telegram_bot_token="12345:fake_token",  # noqa: S106 - deterministic fake token.
        telegram_chat_id="safe-chat",
        analytics_version_http_timeout_seconds=0.4,
        telegram_request_timeout_seconds=2.5,
        daily_admission_check_interval_seconds=17,
        daily_admission_attempt_timeout_seconds=11,
        daily_admission_retry_delays_seconds=(2, 5),
        readiness_alert_poll_interval_seconds=12,
        readiness_alert_startup_grace_seconds=70,
        readiness_alert_heartbeat_confirmation_seconds=32,
        readiness_alert_control_confirmation_seconds=33,
        readiness_alert_outbox_confirmation_seconds=63,
        readiness_alert_retry_delays_seconds=(4, 8),
        readiness_alert_queue_limit=19,
    )
    monkeypatch.setattr(app_module, "Settings", lambda: settings)
    analytics_calls = []
    transport_calls = []
    alert_calls = []
    daily_calls = []
    actual_analytics_client = app_module.AnalyticsVersionClient
    actual_transport_factory = app_module.create_telegram_alert_transport
    actual_alert_service = app_module.TradingReadinessAlertService
    actual_daily_service = composition_module.DailyAdmissionPreparationService

    def capture_analytics(*args, **kwargs):
        analytics_calls.append(kwargs)
        return actual_analytics_client(*args, **kwargs)

    def capture_transport(**kwargs):
        transport_calls.append(kwargs)
        return actual_transport_factory(**kwargs)

    def capture_alert_service(*args, **kwargs):
        alert_calls.append(kwargs)
        return actual_alert_service(*args, **kwargs)

    def capture_daily_service(*args, **kwargs):
        daily_calls.append(kwargs)
        return actual_daily_service(*args, **kwargs)

    monkeypatch.setattr(app_module, "AnalyticsVersionClient", capture_analytics)
    monkeypatch.setattr(app_module, "create_telegram_alert_transport", capture_transport)
    monkeypatch.setattr(app_module, "TradingReadinessAlertService", capture_alert_service)
    monkeypatch.setattr(composition_module, "DailyAdmissionPreparationService", capture_daily_service)
    with TestClient(app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'alerts.db'}")):
        pass

    assert analytics_calls == [{"timeout_seconds": 0.4}]
    assert daily_calls[0]["check_interval_seconds"] == 17
    assert daily_calls[0]["attempt_timeout_seconds"] == 11
    assert daily_calls[0]["retry_delays_seconds"] == (2.0, 5.0)
    assert transport_calls[0]["request_timeout_seconds"] == 2.5
    assert alert_calls[0] == {
        "environment": "TEST",
        "enabled": True,
        "poll_interval_seconds": 12,
        "startup_grace_seconds": 70,
        "heartbeat_confirmation_seconds": 32,
        "control_confirmation_seconds": 33,
        "outbox_confirmation_seconds": 63,
        "retry_delays_seconds": (4.0, 8.0),
        "queue_limit": 19,
    }
