"""PostgreSQL upgrade and safe rollback for per-automation order quota settings."""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.database import create_session_factory
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.schema_revision import current_schema_revision, expected_schema_revision
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    rendered_url = database_url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered_url)
    return config


@pytest.mark.postgresql
def test_postgresql_quota_migration_preserves_config_and_requires_clear_before_downgrade(
    isolated_postgresql_database_url: URL,
) -> None:
    config = _config(isolated_postgresql_database_url)
    command.downgrade(config, "0004_trading_admission")
    engine = create_engine(isolated_postgresql_database_url)
    factory = create_session_factory(engine)
    try:
        assert "order_quota_percent" not in {
            column["name"] for column in inspect(engine).get_columns("trading_automations")
        }
        command.upgrade(config, "head")
        with factory.begin() as session:
            session.add(user_broker_model("scope-1", "account-1"))
            session.flush()
            session.add(instrument_model("instrument-1", "scope-1"))
            session.flush()
            session.add(automation_model("automation-1", state="IN_WORK"))

        repository = AutomationRepository(factory)
        repository.update_settings(
            "automation-1",
            expected_revision=1,
            expected_settings_revision=0,
            order_quota_percent=Decimal("10"),
        )
        with pytest.raises(RuntimeError, match="configured percentage"):
            command.downgrade(config, "0004_trading_admission")
        assert current_schema_revision(engine) == expected_schema_revision()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT order_quota_percent FROM trading_automations")) == Decimal("10")

        repository.update_settings(
            "automation-1",
            expected_revision=1,
            expected_settings_revision=1,
            order_quota_percent=None,
        )
        command.downgrade(config, "0004_trading_admission")

        assert current_schema_revision(engine) == "0004_trading_admission"
        columns = {column["name"] for column in inspect(engine).get_columns("trading_automations")}
        assert "order_quota_percent" not in columns
        assert "settings_revision" not in columns
    finally:
        engine.dispose()
