"""Worker-owned SQLite engine configuration."""

import sqlite3

from sqlalchemy import Engine, create_engine, event


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
