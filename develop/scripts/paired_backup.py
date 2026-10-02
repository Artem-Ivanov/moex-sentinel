#!/usr/bin/env python3
"""Paired backup only. Quiesce both Workers and Core writers before invoking.

Never restores or stops services. PostgreSQL uses PG* environment/libpq credentials;
no credential URL is placed in process arguments. Use pg_dump/pg_restore matching
the PostgreSQL server major for the restore rehearsal. Keep the bundle outside Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def backup_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    with destination.open("xb"):
        destination.chmod(0o600)
    try:
        with (
            closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(destination)) as copied,
        ):
            live.backup(copied)
            if copied.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite backup integrity check failed")  # noqa: TRY301
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def checksum(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, action="append", required=True)
    parser.add_argument("--writers-quiesced", action="store_true", required=True)
    args = parser.parse_args()
    # Sequential snapshots are a consistent pair only while all writers remain stopped.
    args.output.mkdir(mode=0o700)
    args.output.chmod(0o700)
    try:
        subprocess.run(
            [
                shutil.which("pg_dump") or "/usr/bin/pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(args.output / "core.dump"),
            ],
            check=True,
            capture_output=True,
        )
        (args.output / "core.dump").chmod(0o600)
        for index, source in enumerate(args.worker):
            backup_sqlite(source, args.output / f"worker-{index}.sqlite")
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "writers_quiesced": True,
            "files": {file.name: checksum(file) for file in args.output.iterdir()},
        }
        path = args.output / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        path.chmod(0o600)
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit("BACKUP_FAILED: bundle incomplete; retain sources and retry into a new directory") from None
    print("BACKUP_CREATED: restore rehearsal is still required")  # noqa: T201


if __name__ == "__main__":
    main()
