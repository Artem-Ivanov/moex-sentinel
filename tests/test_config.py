import pytest
from pydantic import ValidationError

from moex_sentinel.config import Settings


def test_snapshot_worker_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.portfolio_snapshot_interval_seconds == 60
    assert settings.portfolio_snapshot_retry_limit == 3
    assert settings.portfolio_snapshot_retry_base_seconds == 1.0


def test_core_operational_environment_uses_contract_names_and_defaults(monkeypatch) -> None:
    fields = {
        "DAILY_ADMISSION_CHECK_INTERVAL_SECONDS": 60,
        "DAILY_ADMISSION_ATTEMPT_TIMEOUT_SECONDS": 30,
        "DAILY_ADMISSION_RETRY_DELAYS_SECONDS": [60, 120, 300],
        "READINESS_ALERT_POLL_INTERVAL_SECONDS": 10,
        "READINESS_ALERT_STARTUP_GRACE_SECONDS": 60,
        "READINESS_ALERT_HEARTBEAT_CONFIRMATION_SECONDS": 30,
        "READINESS_ALERT_CONTROL_CONFIRMATION_SECONDS": 30,
        "READINESS_ALERT_OUTBOX_CONFIRMATION_SECONDS": 60,
        "READINESS_ALERT_RETRY_DELAYS_SECONDS": [30, 60, 300],
        "READINESS_ALERT_QUEUE_LIMIT": 16,
        "TELEGRAM_REQUEST_TIMEOUT_SECONDS": 5,
        "MARKET_RECOVERY_RETRY_BASE_SECONDS": 1,
        "MARKET_RECOVERY_PROBE_INTERVAL_SECONDS": 60,
        "MARKET_OPERATION_TIMEOUT_SECONDS": 10,
        "MARKET_CLOSE_TIMEOUT_SECONDS": 5,
        "MARKET_QUIET_SECONDS": 30,
        "MARKET_CANDLES_REFRESH_SECONDS": 60,
        "ANALYTICS_VERSION_HTTP_TIMEOUT_SECONDS": 1,
    }
    settings = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert settings.daily_admission_check_interval_seconds == fields["DAILY_ADMISSION_CHECK_INTERVAL_SECONDS"]
    assert settings.daily_admission_attempt_timeout_seconds == fields["DAILY_ADMISSION_ATTEMPT_TIMEOUT_SECONDS"]
    assert list(settings.daily_admission_retry_delays_seconds) == fields["DAILY_ADMISSION_RETRY_DELAYS_SECONDS"]
    assert settings.readiness_alert_poll_interval_seconds == fields["READINESS_ALERT_POLL_INTERVAL_SECONDS"]
    assert settings.readiness_alert_startup_grace_seconds == fields["READINESS_ALERT_STARTUP_GRACE_SECONDS"]
    assert (
        settings.readiness_alert_heartbeat_confirmation_seconds
        == fields["READINESS_ALERT_HEARTBEAT_CONFIRMATION_SECONDS"]
    )
    assert (
        settings.readiness_alert_control_confirmation_seconds == fields["READINESS_ALERT_CONTROL_CONFIRMATION_SECONDS"]
    )
    assert settings.readiness_alert_outbox_confirmation_seconds == fields["READINESS_ALERT_OUTBOX_CONFIRMATION_SECONDS"]
    assert list(settings.readiness_alert_retry_delays_seconds) == fields["READINESS_ALERT_RETRY_DELAYS_SECONDS"]
    assert settings.readiness_alert_queue_limit == fields["READINESS_ALERT_QUEUE_LIMIT"]
    assert settings.telegram_request_timeout_seconds == fields["TELEGRAM_REQUEST_TIMEOUT_SECONDS"]
    assert settings.market_recovery_retry_base_seconds == fields["MARKET_RECOVERY_RETRY_BASE_SECONDS"]
    assert settings.market_recovery_probe_interval_seconds == fields["MARKET_RECOVERY_PROBE_INTERVAL_SECONDS"]
    assert settings.market_operation_timeout_seconds == fields["MARKET_OPERATION_TIMEOUT_SECONDS"]
    assert settings.market_close_timeout_seconds == fields["MARKET_CLOSE_TIMEOUT_SECONDS"]
    assert settings.market_quiet_seconds == fields["MARKET_QUIET_SECONDS"]
    assert settings.market_candles_refresh_seconds == fields["MARKET_CANDLES_REFRESH_SECONDS"]
    assert settings.analytics_version_http_timeout_seconds == fields["ANALYTICS_VERSION_HTTP_TIMEOUT_SECONDS"]

    monkeypatch.setenv("DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "2")
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT_SECONDS", "3.25")
    monkeypatch.setenv("DAILY_ADMISSION_CHECK_INTERVAL_SECONDS", "17")
    monkeypatch.setenv("DAILY_ADMISSION_RETRY_DELAYS_SECONDS", "[2.5, 7]")
    monkeypatch.setenv("READINESS_ALERT_POLL_INTERVAL_SECONDS", "12")
    monkeypatch.setenv("READINESS_ALERT_RETRY_DELAYS_SECONDS", "[4, 8]")
    monkeypatch.setenv("READINESS_ALERT_QUEUE_LIMIT", "23")
    monkeypatch.setenv("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "2.75")
    monkeypatch.setenv("MARKET_RECOVERY_RETRY_BASE_SECONDS", "2.5")
    monkeypatch.setenv("MARKET_CANDLES_REFRESH_SECONDS", "15")
    monkeypatch.setenv("ANALYTICS_VERSION_HTTP_TIMEOUT_SECONDS", "0.5")
    configured = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert configured.database_pool_size == 7
    assert configured.database_max_overflow == 2
    assert configured.database_pool_timeout_seconds == 3.25
    assert configured.daily_admission_check_interval_seconds == 17
    assert list(configured.daily_admission_retry_delays_seconds) == [2.5, 7.0]
    assert configured.readiness_alert_poll_interval_seconds == 12
    assert list(configured.readiness_alert_retry_delays_seconds) == [4.0, 8.0]
    assert configured.readiness_alert_queue_limit == 23
    assert configured.telegram_request_timeout_seconds == 2.75
    assert configured.market_recovery_retry_base_seconds == 2.5
    assert configured.market_candles_refresh_seconds == 15
    assert configured.analytics_version_http_timeout_seconds == 0.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("daily_admission_retry_delays_seconds", []),
        ("daily_admission_retry_delays_seconds", [0]),
        ("daily_admission_retry_delays_seconds", [float("inf")]),
        ("readiness_alert_retry_delays_seconds", [float("nan")]),
        ("market_recovery_retry_base_seconds", float("inf")),
        ("market_operation_timeout_seconds", float("nan")),
        ("analytics_version_http_timeout_seconds", 0),
        ("database_pool_timeout_seconds", float("nan")),
        ("database_pool_timeout_seconds", float("inf")),
        ("portfolio_snapshot_retry_base_seconds", float("nan")),
        ("portfolio_snapshot_retry_base_seconds", float("inf")),
        ("readiness_alert_queue_limit", 0),
        ("readiness_alert_queue_limit", 1025),
    ],
)
def test_core_operational_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, broker_access_mode="READ_ONLY", **{field: value})


def test_daily_admission_preparation_is_opt_in_without_canary_configuration() -> None:
    settings = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert settings.daily_admission_preparation_enabled is False
    assert not hasattr(settings, "daily_admission_canary_uid")

    enabled = Settings(
        _env_file=None,
        broker_access_mode="TRADE",
        daily_admission_preparation_enabled=True,
    )
    assert enabled.daily_admission_preparation_enabled is True
    assert not hasattr(enabled, "daily_admission_canary_uid")


def test_readiness_alerts_are_disabled_and_secret_is_hidden_by_default() -> None:
    settings = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert settings.telegram_alerts_enabled is False
    assert settings.telegram_bot_token.get_secret_value() == ""
    assert settings.telegram_chat_id == ""
    assert "123456:secret-token" not in repr(
        Settings(
            _env_file=None,
            broker_access_mode="READ_ONLY",
            telegram_bot_token="123456:secret-token",  # noqa: S106 - deterministic fake bot token.
        )
    )


def test_enabled_readiness_alerts_require_complete_valid_credentials() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, broker_access_mode="READ_ONLY", telegram_alerts_enabled=True)
    configured = Settings(
        _env_file=None,
        broker_access_mode="READ_ONLY",
        telegram_alerts_enabled=True,
        telegram_bot_token="123456:secret_token-1",  # noqa: S106 - deterministic fake bot token.
        telegram_chat_id="-1001234567890",
    )
    assert configured.telegram_alerts_enabled is True


@pytest.mark.parametrize("token", ["plain-secret", "123456:secret.token", "bad\ntoken"])
def test_telegram_bot_token_rejects_malformed_values(token: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, broker_access_mode="READ_ONLY", telegram_bot_token=token)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("portfolio_snapshot_interval_seconds", 59),
        ("portfolio_snapshot_retry_limit", -1),
        ("portfolio_snapshot_retry_base_seconds", 0),
    ],
)
def test_snapshot_worker_settings_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_core_analytics_url_has_no_cross_contour_default(monkeypatch):
    monkeypatch.delenv("ANALYTICS_URL", raising=False)
    assert Settings(_env_file=None, broker_access_mode="READ_ONLY").analytics_url == ""


def test_worker_credentials_are_optional_in_test_but_required_and_hidden_in_prod() -> None:
    test = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert test.internal_worker_id == ""
    assert test.internal_worker_broker_id is None
    assert test.internal_worker_token.get_secret_value() == ""

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            application_environment="PROD",
            broker_access_mode="READ_ONLY",
            auth_allowed_origin="https://operator.example.test",
            auth_session_cookie_name="__Host-prod-operator",
        )

    secret = "a" * 64
    prod = Settings(
        _env_file=None,
        application_environment="PROD",
        broker_access_mode="READ_ONLY",
        auth_allowed_origin="https://operator.example.test",
        auth_session_cookie_name="__Host-prod-operator",
        internal_worker_id="worker-prod-1",
        internal_worker_broker_id="00000000-0000-4000-8000-000000000001",
        internal_worker_token=secret,
    )
    assert prod.internal_worker_token.get_secret_value() == secret
    assert secret not in repr(prod)


def test_prod_trade_requires_scoped_worker_credentials_but_is_configurable() -> None:
    settings = Settings(
        _env_file=None,
        application_environment="PROD",
        broker_access_mode="TRADE",
        auth_allowed_origin="https://operator.example.test",
        auth_session_cookie_name="__Host-prod-operator",
        internal_worker_id="worker-prod-1",
        internal_worker_broker_id="00000000-0000-4000-8000-000000000001",
        internal_worker_token="a" * 64,
    )

    assert settings.broker_access_mode == "TRADE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("internal_worker_id", " worker "),
        ("internal_worker_id", "bad\nworker"),
        ("internal_worker_token", "short"),
        ("internal_worker_token", "A" * 64),
        ("internal_worker_broker_id", "not-a-uuid"),
    ],
)
def test_worker_config_rejects_malformed_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, broker_access_mode="READ_ONLY", **{field: value})
