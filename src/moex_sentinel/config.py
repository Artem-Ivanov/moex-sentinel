"""Application configuration."""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


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
    portfolio_snapshot_interval_seconds: int = Field(default=60, ge=60)
    portfolio_snapshot_retry_limit: int = Field(default=3, ge=0)
    portfolio_snapshot_retry_base_seconds: float = Field(default=1.0, gt=0)
    log_level: str = "INFO"
    log_format: str = "json"

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if make_url(self.database_url).get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("Only SQLite or PostgreSQL database URLs are supported.")
        if self.database_pool_size <= 0 or self.database_max_overflow < 0 or self.database_pool_timeout_seconds <= 0:
            raise ValueError("database pool configuration is invalid.")
        if self.log_format not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be json or console.")
        return self
