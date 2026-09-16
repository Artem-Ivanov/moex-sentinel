"""Worker-owned SQLite engine configuration."""

import sqlite3

from sqlalchemy import Engine, create_engine, event

from trading_automaton.storage.models import TRADE_DECISION_INTENT_INDEX, Base


def initialize_worker_schema(engine: Engine) -> None:
    """Create Worker tables and add the decision lookup index to existing databases without replacing rows."""
    Base.metadata.create_all(engine)
    TRADE_DECISION_INTENT_INDEX.create(engine, checkfirst=True)


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
