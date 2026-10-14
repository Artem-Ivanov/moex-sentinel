"""Application configuration."""

import re
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
    daily_admission_canary_uid: str = ""
    telegram_alerts_enabled: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

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

    @field_validator("daily_admission_canary_uid")
    @classmethod
    def validate_daily_admission_canary_uid(cls, value: str) -> str:
        if value and (
            len(value) > 128
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("DAILY_ADMISSION_CANARY_UID has invalid format.")
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
