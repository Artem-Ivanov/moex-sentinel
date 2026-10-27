"""Application configuration."""

import re
from math import isfinite
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment


class Settings(BaseSettings):
    """Validated local application settings."""

    model_config = SettingsConfigDict(
        env_file="../../.env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    database_url: str = "sqlite:///data/moex_sentinel.db"
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout_seconds: float = 5.0
    daily_admission_check_interval_seconds: int = Field(default=60, gt=0)
    daily_admission_attempt_timeout_seconds: int = Field(default=30, gt=0)
    daily_admission_retry_delays_seconds: tuple[float, ...] = (60.0, 120.0, 300.0)
    readiness_alert_poll_interval_seconds: int = Field(default=10, gt=0)
    readiness_alert_startup_grace_seconds: int = Field(default=60, gt=0)
    readiness_alert_heartbeat_confirmation_seconds: int = Field(default=30, gt=0)
    readiness_alert_control_confirmation_seconds: int = Field(default=30, gt=0)
    readiness_alert_outbox_confirmation_seconds: int = Field(default=60, gt=0)
    readiness_alert_retry_delays_seconds: tuple[float, ...] = (30.0, 60.0, 300.0)
    readiness_alert_queue_limit: int = Field(default=16, gt=0, le=1024)
    telegram_request_timeout_seconds: float = Field(default=5.0, gt=0)
    market_recovery_retry_base_seconds: float = Field(default=1.0, gt=0)
    market_recovery_probe_interval_seconds: int = Field(default=60, gt=0)
    market_operation_timeout_seconds: float = Field(default=10.0, gt=0)
    market_close_timeout_seconds: float = Field(default=5.0, gt=0)
    market_quiet_seconds: float = Field(default=30.0, gt=0)
    market_trading_status_ttl_seconds: float = Field(default=30.0, gt=0)
    market_candles_refresh_seconds: float = Field(default=60.0, gt=0)
    analytics_version_http_timeout_seconds: float = Field(default=1.0, gt=0)
    sandbox_retry_limit: int = Field(default=5, ge=0)
    portfolio_snapshot_all_environments: bool = False
    portfolio_snapshot_interval_seconds: int = Field(default=60, ge=60)
    portfolio_snapshot_retry_limit: int = Field(default=3, ge=0)
    portfolio_snapshot_retry_base_seconds: float = Field(default=1.0, gt=0)
    log_level: str = "INFO"
    log_format: str = "json"
    application_environment: BrokerEnvironment = "TEST"
    broker_access_mode: BrokerAccessMode
    analytics_url: str = ""
    auth_session_cookie_name: str = "__Host-moex-session"
    auth_allowed_origin: str = ""
    auth_username: str = ""
    auth_password_hash: str = ""
    auth_insecure_loopback: bool = False
    internal_worker_id: str = ""
    internal_worker_broker_id: UUID | None = None
    internal_worker_token: SecretStr = SecretStr("")
    daily_admission_preparation_enabled: bool = False
    telegram_alerts_enabled: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    @field_validator("daily_admission_retry_delays_seconds", "readiness_alert_retry_delays_seconds")
    @classmethod
    def validate_retry_delays(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value or any(not isfinite(delay) or delay <= 0 for delay in value):
            raise ValueError("Retry delays must be nonempty, finite and positive.")
        return value

    @field_validator("internal_worker_broker_id", mode="before")
    @classmethod
    def empty_worker_broker_id_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("internal_worker_id")
    @classmethod
    def validate_internal_worker_id(cls, value: str) -> str:
        if value and (
            len(value) > 128
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("INTERNAL_WORKER_ID has invalid format.")
        return value

    @field_validator("internal_worker_token")
    @classmethod
    def validate_internal_worker_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if token and re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("INTERNAL_WORKER_TOKEN must be 64 lowercase hexadecimal characters.")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_telegram_bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if token and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) is None:
            raise ValueError("TELEGRAM_BOT_TOKEN has invalid format.")
        return value

    @field_validator("telegram_chat_id")
    @classmethod
    def validate_telegram_chat_id(cls, value: str) -> str:
        if value and (
            len(value) > 128
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("TELEGRAM_CHAT_ID has invalid format.")
        return value

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if make_url(self.database_url).get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("Only SQLite or PostgreSQL database URLs are supported.")
        if self.database_pool_size <= 0 or self.database_max_overflow < 0 or self.database_pool_timeout_seconds <= 0:
            raise ValueError("database pool configuration is invalid.")
        operational_floats = (
            self.database_pool_timeout_seconds,
            self.portfolio_snapshot_retry_base_seconds,
            self.telegram_request_timeout_seconds,
            self.market_recovery_retry_base_seconds,
            self.market_operation_timeout_seconds,
            self.market_close_timeout_seconds,
            self.market_quiet_seconds,
            self.market_trading_status_ttl_seconds,
            self.market_candles_refresh_seconds,
            self.analytics_version_http_timeout_seconds,
        )
        if any(not isfinite(value) or value <= 0 for value in operational_floats):
            raise ValueError("operational timing configuration is invalid.")
        if self.log_format not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be json or console.")
        if self.telegram_alerts_enabled and (
            not self.telegram_bot_token.get_secret_value() or not self.telegram_chat_id
        ):
            raise ValueError("Enabled Telegram alerts require TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        if self.application_environment == "PROD":
            origin = urlsplit(self.auth_allowed_origin)
            if (
                origin.scheme != "https"
                or not origin.netloc
                or origin.path
                or origin.query
                or origin.fragment
                or origin.username
            ):
                raise ValueError("PROD requires an exact HTTPS AUTH_ALLOWED_ORIGIN.")
            if (
                self.auth_insecure_loopback
                or not self.auth_session_cookie_name.startswith("__Host-")
                or self.auth_session_cookie_name == "__Host-moex-session"
            ):
                raise ValueError("PROD requires a distinct secure session cookie.")
            if not self.internal_worker_id or self.internal_worker_broker_id is None:
                raise ValueError("PROD requires INTERNAL_WORKER_ID and INTERNAL_WORKER_BROKER_ID.")
            if not self.internal_worker_token.get_secret_value():
                raise ValueError("PROD requires INTERNAL_WORKER_TOKEN.")
        return self
