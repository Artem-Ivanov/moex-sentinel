"""Worker-owned SQLite engine configuration."""

import sqlite3

from sqlalchemy import Engine, create_engine, event, inspect

from trading_automaton.storage.models import TRADE_DECISION_INTENT_INDEX, Base
from trading_automaton.storage.state_schema import migrate_state_checks


def initialize_worker_schema(engine: Engine) -> None:
    """Upgrade private SQLite atomically while preserving existing rows and extensions."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        migrate_state_checks(connection)
        Base.metadata.create_all(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("cached_automations")}
        if "settings_revision" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE cached_automations ADD COLUMN settings_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "order_quota_percent" not in columns:
            connection.exec_driver_sql("ALTER TABLE cached_automations ADD COLUMN order_quota_percent NUMERIC(28, 9)")
        TRADE_DECISION_INTENT_INDEX.create(connection, checkfirst=True)
        connection.commit()


def downgrade_worker_states(engine: Engine) -> None:
    """Remove only revision 1 CHECKs; retain quota, financial rows and later extensions."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        migrate_state_checks(connection, downgrade=True)
        connection.commit()


def downgrade_worker_execution_commission_cache(engine: Engine) -> None:
    """Drop only an empty fee projection; trusted amounts are financial history."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if "execution_commission_cache" in inspect(connection).get_table_names():
            count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM execution_commission_cache "
                "WHERE effective_broker_commission IS NOT NULL"
            ).scalar_one()
            if count:
                connection.rollback()
                raise RuntimeError("Refusing to remove trusted execution commission cache values.")
            connection.exec_driver_sql("DROP TABLE execution_commission_cache")
        connection.commit()


def create_worker_engine(database_url: str) -> Engine:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Trading worker supports only its private SQLite database.")
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine
