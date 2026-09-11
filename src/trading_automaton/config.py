"""Trading-worker runtime and strategy configuration."""

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator
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
    enabled: bool = Field(True, validation_alias="STRATEGY_ENABLED")


class AutomatonSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    core_url: str = Field("http://backend:8000", validation_alias="CORE_URL")
    analytics_url: str = Field("http://analytics:8001", validation_alias="ANALYTICS_URL")
    database_url: str = Field(
        "sqlite:///data/trading_automaton.db",
        validation_alias="AUTOMATON_DATABASE_URL",
    )
    worker_id: str = Field("trading-automaton-1", validation_alias="AUTOMATON_WORKER_ID")
    iteration_seconds: float = Field(1.0, validation_alias="AUTOMATON_ITERATION_SECONDS")
    sandbox_retry_limit: int = Field(5, validation_alias="SANDBOX_RETRY_LIMIT", ge=0)
    heartbeat_interval_seconds: float = Field(3.0, validation_alias="AUTOMATON_HEARTBEAT_INTERVAL_SECONDS")
    fact_outbox_batch_size: int = Field(100, validation_alias="FACT_OUTBOX_BATCH_SIZE")
    fact_outbox_deadline_ms: int = Field(1000, validation_alias="FACT_OUTBOX_DEADLINE_MS")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field("json", validation_alias="LOG_FORMAT")

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Trading worker supports only SQLite.")
        if self.iteration_seconds <= 0:
            raise ValueError("Worker iteration interval must be positive.")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("Worker heartbeat interval must be positive.")
        if self.fact_outbox_batch_size <= 0:
            raise ValueError("Fact outbox batch size must be positive.")
        if self.fact_outbox_deadline_ms <= 0:
            raise ValueError("Fact outbox deadline must be positive.")
        if self.log_format not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be json or console.")
        return self
