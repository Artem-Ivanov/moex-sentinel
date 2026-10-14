import pytest
from pydantic import ValidationError

from moex_sentinel.config import Settings


def test_snapshot_worker_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.portfolio_snapshot_interval_seconds == 60
    assert settings.portfolio_snapshot_retry_limit == 3
    assert settings.portfolio_snapshot_retry_base_seconds == 1.0


def test_daily_admission_preparation_is_opt_in_and_empty_canary_means_baseline_only() -> None:
    settings = Settings(_env_file=None, broker_access_mode="READ_ONLY")
    assert settings.daily_admission_preparation_enabled is False
    assert settings.daily_admission_canary_uid == ""

    enabled = Settings(
        _env_file=None,
        broker_access_mode="TRADE",
        daily_admission_preparation_enabled=True,
        daily_admission_canary_uid="uid-canary",
    )
    assert enabled.daily_admission_preparation_enabled is True
    assert enabled.daily_admission_canary_uid == "uid-canary"


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


@pytest.mark.parametrize("uid", [" canary ", "bad\nuid", "x" * 129])
def test_daily_admission_canary_uid_rejects_ambiguous_or_oversized_values(uid: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, broker_access_mode="READ_ONLY", daily_admission_canary_uid=uid)


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
