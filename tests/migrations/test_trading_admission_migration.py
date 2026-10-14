from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.schema_revision import current_schema_revision, expected_schema_revision


def test_admission_revision_is_head_and_creates_durable_guard_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admission.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        assert current_schema_revision(engine) == expected_schema_revision()
        expected = {
            "worker_runtime_instances",
            "trading_account_admissions",
            "trading_admission_baselines",
            "trading_admission_reconciliations",
            "trading_submit_reservations",
            "trading_admission_events",
        }
        assert expected <= set(inspect(engine).get_table_names())
        assert {"uq_trading_baseline_scope_day"} <= {
            item["name"] for item in inspect(engine).get_unique_constraints("trading_admission_baselines")
        }
        assert {"uq_trading_reservation_scope_key"} <= {
            item["name"] for item in inspect(engine).get_unique_constraints("trading_submit_reservations")
        }
    finally:
        engine.dispose()


def test_admission_downgrade_refuses_nonempty_durable_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admission-data.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_brokers "
                    "(id, api_slug, display_name, environment, fqdn, settings, external_account_id, state, "
                    "created_at, updated_at) VALUES "
                    "('scope', 'api', 'Synthetic', 'PROD', 'example.test', '{}', 'account', 'ACTIVE', "
                    "'2026-10-09T00:00:00Z', '2026-10-09T00:00:00Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO trading_admission_events "
                    "(event_id, user_broker_id, event_type, occurred_at, payload) VALUES "
                    "('event', 'scope', 'RUNTIME_REGISTERED', '2026-10-09T00:00:00Z', '{}')"
                )
            )
        with pytest.raises(RuntimeError, match="Cannot remove trading admission history"):
            command.downgrade(config, "0003_user_broker_archive")
        assert current_schema_revision(engine) == "0004_trading_admission"
    finally:
        engine.dispose()
