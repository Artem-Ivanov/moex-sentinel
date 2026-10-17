"""Trading-worker runtime and strategy configuration."""

from decimal import Decimal
from math import isfinite
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrategySettings(BaseSettings):
    """Immutable code-owned strategy parameters shared by all automations."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    buy_order_lots: int = Field(1, validation_alias="STRATEGY_BUY_ORDER_LOTS", gt=0)
    stop_loss_percent: Decimal = Field(
        Decimal("5"),
        validation_alias="STRATEGY_STOP_LOSS_PERCENT",
        gt=0,
        le=100,
    )
    take_profit_percent: Decimal = Field(
        Decimal("6"),
        validation_alias="STRATEGY_TAKE_PROFIT_PERCENT",
        gt=0,
        le=100,
    )
    averaging_step_percent: Decimal = Field(
        Decimal("0.5"),
        validation_alias="STRATEGY_AVERAGING_STEP_PERCENT",
        gt=0,
        le=100,
    )
    partial_take_profit_percent: Decimal = Field(
        Decimal("0.5"),
        validation_alias="STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT",
        gt=0,
        le=100,
    )
    partial_sell_percent: Decimal = Field(
        Decimal("25"),
        validation_alias="STRATEGY_PARTIAL_SELL_PERCENT",
        gt=0,
        le=100,
    )
    max_partial_sell_steps: int = Field(
        3,
        validation_alias="STRATEGY_MAX_PARTIAL_SELL_STEPS",
        ge=0,
    )
    order_ttl_seconds: int = Field(
        10,
        validation_alias="STRATEGY_ORDER_TTL_SECONDS",
        gt=0,
    )
    order_retry_limit: int = Field(
        3,
        validation_alias="STRATEGY_ORDER_RETRY_LIMIT",
        ge=0,
    )
    core_retry_limit: int = Field(
        5,
        validation_alias="STRATEGY_CORE_RETRY_LIMIT",
        ge=0,
    )
    enabled: bool = Field(False, validation_alias="STRATEGY_ENABLED")


class AutomatonSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, hide_input_in_errors=True)

    application_environment: Literal["TEST", "PROD"] = Field("TEST", validation_alias="APPLICATION_ENVIRONMENT")
    broker_access_mode: Literal["READ_ONLY", "TRADE"] = Field(validation_alias="BROKER_ACCESS_MODE")

    core_url: str = Field("http://backend:8000", validation_alias="CORE_URL")
    analytics_url: str = Field("http://analytics:8001", validation_alias="ANALYTICS_URL")
    database_url: str = Field(
        "sqlite:///data/trading_automaton.db",
        validation_alias="AUTOMATON_DATABASE_URL",
    )
    worker_id: str = Field("trading-automaton-1", validation_alias="AUTOMATON_WORKER_ID")
    internal_worker_id: str | None = Field(None, validation_alias="INTERNAL_WORKER_ID")
    internal_worker_broker_id: UUID | None = Field(None, validation_alias="INTERNAL_WORKER_BROKER_ID")
    internal_worker_token: SecretStr | None = Field(None, validation_alias="INTERNAL_WORKER_TOKEN")
    iteration_seconds: float = Field(1.0, validation_alias="AUTOMATON_ITERATION_SECONDS")
    sandbox_retry_limit: int = Field(5, validation_alias="SANDBOX_RETRY_LIMIT", ge=0)
    heartbeat_interval_seconds: float = Field(3.0, validation_alias="AUTOMATON_HEARTBEAT_INTERVAL_SECONDS")
    core_http_timeout_seconds: float = Field(10.0, validation_alias="AUTOMATON_CORE_HTTP_TIMEOUT_SECONDS", gt=0)
    analytics_http_timeout_seconds: float = Field(
        1.0, validation_alias="AUTOMATON_ANALYTICS_HTTP_TIMEOUT_SECONDS", gt=0
    )
    broker_recovery_probe_seconds: float = Field(60.0, validation_alias="AUTOMATON_BROKER_RECOVERY_PROBE_SECONDS", gt=0)
    account_snapshot_ttl_seconds: float = Field(60.0, validation_alias="AUTOMATON_ACCOUNT_SNAPSHOT_TTL_SECONDS", gt=0)
    fact_outbox_batch_size: int = Field(100, validation_alias="FACT_OUTBOX_BATCH_SIZE")
    fact_outbox_deadline_ms: int = Field(1000, validation_alias="FACT_OUTBOX_DEADLINE_MS")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field("json", validation_alias="LOG_FORMAT")

    @field_validator("core_url", "analytics_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        error = "Service URL must be an absolute HTTP(S) URL with a hostname and no credentials."
        if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(error)
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError(error) from None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
            or (port is not None and not 0 <= port <= 65535)
        ):
            raise ValueError(error)
        return value

    @field_validator("internal_worker_id", "internal_worker_broker_id", "internal_worker_token", mode="before")
    @classmethod
    def empty_identity_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.application_environment == "PROD":
            token = None if self.internal_worker_token is None else self.internal_worker_token.get_secret_value()
            if (
                self.internal_worker_id != self.worker_id
                or self.internal_worker_broker_id is None
                or token is None
                or len(token) != 64
                or any(character not in "0123456789abcdef" for character in token)
            ):
                raise ValueError("PROD requires Core worker credentials matching AUTOMATON_WORKER_ID")
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Trading worker supports only SQLite.")
        for name in (
            "iteration_seconds",
            "heartbeat_interval_seconds",
            "core_http_timeout_seconds",
            "analytics_http_timeout_seconds",
            "broker_recovery_probe_seconds",
            "account_snapshot_ttl_seconds",
        ):
            if not isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.fact_outbox_batch_size <= 0:
            raise ValueError("Fact outbox batch size must be positive.")
        if self.fact_outbox_deadline_ms <= 0:
            raise ValueError("Fact outbox deadline must be positive.")
        if self.log_format not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be json or console.")
        return self
