from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.models import AutomationEventModel
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.schema_revision import current_schema_revision
from tests.storage.trading_facts_helpers import (
    NOW,
    automation_model,
    instrument_model,
    position_cycle_model,
    user_broker_model,
)


def _config(database_url: str) -> Config:
    value = Config("alembic.ini")
    value.set_main_option("sqlalchemy.url", database_url)
    return value


def test_downgrade_refuses_to_drop_a_saved_quota_and_allows_cleared_value(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'quota-migration.db'}"
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        quota_column = next(
            column
            for column in inspect(engine).get_columns("trading_automations")
            if column["name"] == "order_quota_percent"
        )
        assert quota_column["type"].precision == 28
        assert quota_column["type"].scale == 9
        with sessionmaker(engine, expire_on_commit=False).begin() as session:
            session.add(user_broker_model("scope-1", "account-1"))
            session.flush()
            session.add(instrument_model("instrument-1", "scope-1"))
            session.flush()
            session.add(automation_model("automation-1", state="IN_WORK"))
        factory = sessionmaker(engine, expire_on_commit=False)
        AutomationRepository(factory).update_settings(
            "automation-1",
            expected_revision=1,
            expected_settings_revision=0,
            order_quota_percent=Decimal("10"),
        )

        with pytest.raises(RuntimeError, match="configured percentage"):
            command.downgrade(config, "0004_trading_admission")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT order_quota_percent FROM trading_automations")) == 10
            assert current_schema_revision(engine) == "0005_order_quota_percent"

        with engine.begin() as connection:
            connection.execute(text("UPDATE trading_automations SET order_quota_percent = NULL"))
        command.downgrade(config, "0004_trading_admission")

        assert current_schema_revision(engine) == "0004_trading_admission"
        assert "order_quota_percent" not in {
            column["name"] for column in inspect(engine).get_columns("trading_automations")
        }
        assert "settings_revision" not in {
            column["name"] for column in inspect(engine).get_columns("trading_automations")
        }
    finally:
        engine.dispose()


def test_migration_keeps_existing_position_cycle_and_allows_safe_rollback(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'quota-position-migration.db'}"
    config = _config(database_url)
    command.upgrade(config, "0004_trading_admission")
    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            session.add(user_broker_model("scope-1", "account-1"))
            session.flush()
            session.add(instrument_model("instrument-1", "scope-1"))
            session.flush()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO trading_automations "
                    "(user_broker_id, instrument_id, state, revision, last_sequence_number, "
                    "resume_requested, id, created_at, updated_at) "
                    "VALUES ('scope-1', 'instrument-1', 'IN_WORK', 1, 0, false, 'automation-1', "
                    "'2026-08-12T12:00:00Z', '2026-08-12T12:00:00Z')"
                )
            )
        with factory.begin() as session:
            session.add(position_cycle_model("cycle-1"))
            session.add(
                AutomationEventModel(
                    event_id="event-1",
                    automation_id="automation-1",
                    user_broker_id="scope-1",
                    sequence_number=1,
                    expected_revision=1,
                    fact_kind="TEST_FACT",
                    safe_message="Synthetic historical event",
                    payload={},
                    occurred_at=NOW,
                    received_at=NOW,
                )
            )

        command.upgrade(config, "head")
        repository = AutomationRepository(factory)
        repository.update_settings(
            "automation-1",
            expected_revision=1,
            expected_settings_revision=0,
            order_quota_percent=Decimal("10"),
        )
        repository.update_settings(
            "automation-1",
            expected_revision=1,
            expected_settings_revision=1,
            order_quota_percent=None,
        )
        command.downgrade(config, "0004_trading_admission")

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT id FROM position_cycles")) == "cycle-1"
            assert connection.scalar(text("SELECT id FROM trading_automations")) == "automation-1"
            assert connection.scalar(text("SELECT event_id FROM automation_events")) == "event-1"
    finally:
        engine.dispose()
