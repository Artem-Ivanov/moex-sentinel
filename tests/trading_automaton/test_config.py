from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_automaton.config import AutomatonSettings, StrategySettings

STRATEGY_ENV_NAMES = (
    "STRATEGY_BUY_ORDER_LOTS",
    "STRATEGY_STOP_LOSS_PERCENT",
    "STRATEGY_TAKE_PROFIT_PERCENT",
    "STRATEGY_AVERAGING_STEP_PERCENT",
    "STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT",
    "STRATEGY_PARTIAL_SELL_PERCENT",
    "STRATEGY_MAX_PARTIAL_SELL_STEPS",
    "STRATEGY_ORDER_TTL_SECONDS",
    "STRATEGY_ORDER_RETRY_LIMIT",
    "STRATEGY_CORE_RETRY_LIMIT",
    "STRATEGY_ENABLED",
)


def test_settings_read_runtime_environment(monkeypatch) -> None:
    monkeypatch.setenv("CORE_URL", "http://core:8000")
    monkeypatch.setenv("AUTOMATON_DATABASE_URL", "sqlite:////tmp/worker.db")
    monkeypatch.setenv("AUTOMATON_WORKER_ID", "worker-a")
    monkeypatch.setenv("AUTOMATON_HEARTBEAT_INTERVAL_SECONDS", "3")

    settings = AutomatonSettings()

    assert settings.core_url == "http://core:8000"
    assert settings.database_url == "sqlite:////tmp/worker.db"
    assert settings.worker_id == "worker-a"
    assert settings.heartbeat_interval_seconds == 3
    assert not hasattr(settings, "fact_ingress_version")
    assert settings.fact_outbox_batch_size == 100
    assert settings.fact_outbox_deadline_ms == 1000


def test_settings_reject_postgresql_for_worker_storage() -> None:
    with pytest.raises(ValidationError, match="supports only SQLite"):
        AutomatonSettings(AUTOMATON_DATABASE_URL="postgresql+psycopg://database/worker")


def test_prod_trade_requires_a_valid_core_worker_credential_tuple() -> None:
    with pytest.raises(ValidationError, match="PROD requires Core worker credentials"):
        AutomatonSettings(
            APPLICATION_ENVIRONMENT="PROD",
            BROKER_ACCESS_MODE="TRADE",
            AUTOMATON_WORKER_ID="worker-a",
            INTERNAL_WORKER_ID="worker-b",
            INTERNAL_WORKER_BROKER_ID="00000000-0000-4000-8000-000000000001",
            INTERNAL_WORKER_TOKEN="a" * 64,
        )


def test_test_settings_allow_empty_production_identity_values() -> None:
    settings = AutomatonSettings(
        APPLICATION_ENVIRONMENT="TEST",
        BROKER_ACCESS_MODE="READ_ONLY",
        INTERNAL_WORKER_ID="",
        INTERNAL_WORKER_BROKER_ID="",
        INTERNAL_WORKER_TOKEN="",
    )
    assert settings.internal_worker_id is None
    assert settings.internal_worker_broker_id is None
    assert settings.internal_worker_token is None


def test_prod_modes_require_authenticated_runtime_identity() -> None:
    with pytest.raises(ValidationError, match="PROD requires Core worker credentials"):
        AutomatonSettings(APPLICATION_ENVIRONMENT="PROD", BROKER_ACCESS_MODE="READ_ONLY")

    settings = AutomatonSettings(
        APPLICATION_ENVIRONMENT="PROD",
        BROKER_ACCESS_MODE="READ_ONLY",
        AUTOMATON_WORKER_ID="worker-a",
        INTERNAL_WORKER_ID="worker-a",
        INTERNAL_WORKER_BROKER_ID="00000000-0000-4000-8000-000000000001",
        INTERNAL_WORKER_TOKEN="a" * 64,
    )
    assert settings.internal_worker_id == "worker-a"
    assert settings.internal_worker_token is not None
    assert "a" * 64 not in repr(settings)


def test_prod_validation_errors_hide_runtime_credentials() -> None:
    secret = "f" * 64
    with pytest.raises(ValidationError) as error:
        AutomatonSettings(
            APPLICATION_ENVIRONMENT="PROD",
            BROKER_ACCESS_MODE="TRADE",
            AUTOMATON_WORKER_ID="worker-a",
            INTERNAL_WORKER_ID="worker-a",
            INTERNAL_WORKER_BROKER_ID="not-a-uuid",
            INTERNAL_WORKER_TOKEN=secret,
        )
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FACT_OUTBOX_BATCH_SIZE", "0"),
        ("FACT_OUTBOX_DEADLINE_MS", "0"),
    ],
)
def test_settings_reject_invalid_outbox_values(name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AutomatonSettings(**{name: value})


def test_strategy_settings_use_approved_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in STRATEGY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    settings = StrategySettings()

    assert settings.buy_order_lots == 1
    assert settings.stop_loss_percent == Decimal("5")
    assert settings.take_profit_percent == Decimal("6")
    assert settings.averaging_step_percent == Decimal("0.5")
    assert settings.partial_take_profit_percent == Decimal("0.5")
    assert settings.partial_sell_percent == Decimal("25")
    assert settings.max_partial_sell_steps == 3
    assert settings.order_ttl_seconds == 10
    assert settings.order_retry_limit == 3
    assert settings.core_retry_limit == 5
    assert settings.enabled is False


def test_strategy_settings_read_complete_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "STRATEGY_BUY_ORDER_LOTS": "2",
        "STRATEGY_STOP_LOSS_PERCENT": "4.5",
        "STRATEGY_TAKE_PROFIT_PERCENT": "7.5",
        "STRATEGY_AVERAGING_STEP_PERCENT": "0.75",
        "STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT": "0.8",
        "STRATEGY_PARTIAL_SELL_PERCENT": "20",
        "STRATEGY_MAX_PARTIAL_SELL_STEPS": "4",
        "STRATEGY_ORDER_TTL_SECONDS": "15",
        "STRATEGY_ORDER_RETRY_LIMIT": "6",
        "STRATEGY_CORE_RETRY_LIMIT": "7",
        "STRATEGY_ENABLED": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = StrategySettings()

    assert settings.buy_order_lots == 2
    assert settings.stop_loss_percent == Decimal("4.5")
    assert settings.take_profit_percent == Decimal("7.5")
    assert settings.averaging_step_percent == Decimal("0.75")
    assert settings.partial_take_profit_percent == Decimal("0.8")
    assert settings.partial_sell_percent == Decimal("20")
    assert settings.max_partial_sell_steps == 4
    assert settings.order_ttl_seconds == 15
    assert settings.order_retry_limit == 6
    assert settings.core_retry_limit == 7
    assert settings.enabled is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STRATEGY_BUY_ORDER_LOTS", "0"),
        ("STRATEGY_STOP_LOSS_PERCENT", "0"),
        ("STRATEGY_TAKE_PROFIT_PERCENT", "100.01"),
        ("STRATEGY_AVERAGING_STEP_PERCENT", "invalid"),
        ("STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT", "-0.1"),
        ("STRATEGY_PARTIAL_SELL_PERCENT", "101"),
        ("STRATEGY_MAX_PARTIAL_SELL_STEPS", "-1"),
        ("STRATEGY_ORDER_TTL_SECONDS", "0"),
        ("STRATEGY_ORDER_RETRY_LIMIT", "-1"),
        ("STRATEGY_CORE_RETRY_LIMIT", "-1"),
        ("STRATEGY_ENABLED", "not-a-bool"),
    ],
)
def test_strategy_settings_reject_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        StrategySettings()


def test_strategy_settings_are_immutable() -> None:
    settings = StrategySettings()

    with pytest.raises(ValidationError):
        settings.buy_order_lots = 2


def test_strategy_settings_accept_explicit_field_overrides() -> None:
    settings = StrategySettings(buy_order_lots=2, averaging_step_percent=Decimal("0.75"))

    assert settings.buy_order_lots == 2
    assert settings.averaging_step_percent == Decimal("0.75")
