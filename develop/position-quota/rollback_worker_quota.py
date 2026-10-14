"""Safely remove Worker order-quota columns from a private SQLite database."""

import sqlite3
import sys
from pathlib import Path


def rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise RuntimeError("Rollback requires a connection with no active transaction.")
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cached_automations)")}
        if not columns:
            raise RuntimeError("Worker cached_automations table is missing; refusing to alter this database.")
        if (
            "order_quota_percent" in columns
            and connection.execute(
                "SELECT 1 FROM cached_automations WHERE order_quota_percent IS NOT NULL LIMIT 1"
            ).fetchone()
        ):
            raise RuntimeError("Worker quota settings exist; refusing to remove saved automation settings.")
        if "order_quota_percent" in columns:
            connection.execute("ALTER TABLE cached_automations DROP COLUMN order_quota_percent")
        if "settings_revision" in columns:
            connection.execute("ALTER TABLE cached_automations DROP COLUMN settings_revision")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: rollback_worker_quota.py /path/to/private-worker.sqlite")
    try:
        database_path = Path(sys.argv[1]).resolve()
        with sqlite3.connect(f"{database_path.as_uri()}?mode=rw", uri=True) as db:
            rollback(db)
    except (RuntimeError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from error
