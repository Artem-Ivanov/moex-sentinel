"""Application configuration."""

from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
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

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if make_url(self.database_url).get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("Only SQLite or PostgreSQL database URLs are supported.")
        if self.database_pool_size <= 0 or self.database_max_overflow < 0 or self.database_pool_timeout_seconds <= 0:
            raise ValueError("database pool configuration is invalid.")
        if self.log_format not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be json or console.")
        if self.application_environment == "PROD":
            if self.broker_access_mode != "READ_ONLY":
                raise ValueError("PROD TRADE requires a separate trading admission.")
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
        return self
