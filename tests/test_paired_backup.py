"""SQLite paired backup preserves pending data without copying a live WAL file."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "paired_backup", Path(__file__).parents[1] / "develop/scripts/paired_backup.py"
)
assert SPEC
assert SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def test_sqlite_backup_includes_committed_wal_and_preserves_source(tmp_path):
    source = tmp_path / "worker.sqlite"
    destination = tmp_path / "backup.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE pending(event_id TEXT PRIMARY KEY, sequence INTEGER)")
        connection.execute("INSERT INTO pending VALUES ('stable-event', 42)")
        connection.commit()
        backup.backup_sqlite(source, destination)
        assert connection.execute("SELECT * FROM pending").fetchall() == [("stable-event", 42)]
    with sqlite3.connect(destination) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT * FROM pending").fetchall() == [("stable-event", 42)]
    assert destination.stat().st_mode & 0o077 == 0


def test_backup_refuses_missing_source_and_existing_destination(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.backup_sqlite(tmp_path / "absent", tmp_path / "backup")
    source = tmp_path / "source"
    with sqlite3.connect(source):
        pass
    target = tmp_path / "target"
    target.write_text("keep")
    with pytest.raises(FileExistsError):
        backup.backup_sqlite(source, target)
    assert target.read_text() == "keep"
