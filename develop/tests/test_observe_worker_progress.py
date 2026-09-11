"""The observer reads aggregate progress without creating or changing its database."""

import sqlite3
from datetime import UTC, datetime

import pytest

from develop.scripts.observe_worker_progress import snapshot


def test_snapshot_reports_backlog_age_and_progress_without_changing_database(tmp_path):
    path = tmp_path / "worker.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE trade_decisions (occurred_at TEXT);"
            "CREATE TABLE fact_outbox (delivery_state TEXT, retry_count INTEGER, created_at TEXT);"
            "INSERT INTO trade_decisions VALUES ('2026-09-09 12:00:01.000000');"
            "INSERT INTO fact_outbox VALUES ('PENDING', 2, '2026-09-09 12:00:00.000000');"
            "INSERT INTO fact_outbox VALUES ('PENDING', 0, '2026-09-09 12:00:02.000000');"
        )
    before = path.read_bytes()
    result = snapshot(path, datetime(2026, 9, 9, 12, 0, 3, tzinfo=UTC))
    assert result["trade_decision_count"] == 1
    assert result["decision_age_ms"] == 2000
    assert result["outbox_state_counts"] == {"PENDING": 2}
    assert result["oldest_pending_age_ms"] == 3000
    assert result["outbox_retry_count_max"] == 2
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        snapshot(path, datetime.now(UTC))
    assert not path.exists()


def test_empty_database_has_no_invented_ages(tmp_path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE trade_decisions (occurred_at TEXT);"
            "CREATE TABLE fact_outbox (delivery_state TEXT, retry_count INTEGER, created_at TEXT);"
        )
    result = snapshot(path, datetime.now(UTC))
    assert result["trade_decision_count"] == 0
    assert result["decision_age_ms"] is None
    assert result["oldest_pending_age_ms"] is None
    assert result["outbox_retry_count_max"] == 0
