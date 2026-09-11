"""Bounded, stopped-Worker recovery for leaked decision_candle_at metadata.

Dry-run is the default. Apply keeps an ID/sequence-only journal in the Worker
SQLite database until every affected queue is acknowledged and reconciled.
The journal permits an identical retry after a lost ACK; accepted payloads are
never edited. This tool never claims commands or talks to a broker.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from sentinel_contracts.trading_facts import AutomationStatus, FactEnvelope
from trading_automaton.adapters.core_client import CoreClient

JOURNAL = "decision_candle_fact_repair"
PRIVATE_KEY = "decision_candle_at"
STATE_FACT = "BROKER_ORDER_STATE_CHANGED"
ADAPTER: TypeAdapter[FactEnvelope] = TypeAdapter(FactEnvelope)


class RepairRefused(RuntimeError):
    """Only fixed, non-sensitive reason codes may be used as messages."""


@dataclass
class Group:
    automation_id: str
    rows: list[sqlite3.Row]
    facts: list[FactEnvelope]
    corrections: dict[str, dict[str, object]]
    first_sequence: int
    last_sequence: int


def _stopped(db: sqlite3.Connection) -> None:
    workers = db.execute("SELECT clean_shutdown FROM worker_runs").fetchall()
    if not workers or any(row[0] != 1 for row in workers):
        raise RepairRefused("WORKER_NOT_CLEANLY_STOPPED")


def _statuses(client: CoreClient, ids: list[str]) -> dict[str, AutomationStatus]:
    result = client.automation_statuses([UUID(value) for value in ids])
    statuses = {str(status.automation_id): status for status in result.automations}
    if result.missing_automation_ids or set(statuses) != set(ids) or len(result.automations) != len(ids):
        raise RepairRefused("CORE_STATUS_INCOMPLETE")
    return statuses


def _journal(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (JOURNAL,)).fetchone()
    if not exists:
        return {}
    return {row["automation_id"]: row for row in db.execute(f"SELECT * FROM {JOURNAL}")}


def _plan(db: sqlite3.Connection, client: CoreClient, max_facts: int) -> list[Group]:
    _stopped(db)
    journal = _journal(db)
    # JSON queries keep this bounded even when unrelated queues are large.
    candidate_ids = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT automation_id FROM fact_outbox "
            "WHERE fact_kind = ? AND delivery_state IN ('FAILED', 'PENDING') "
            "AND json_type(payload, '$.strategy_snapshot.decision_candle_at') IS NOT NULL",
            (STATE_FACT,),
        )
    }
    ids = sorted(candidate_ids | journal.keys())
    if not ids:
        return []
    if len(ids) > max_facts:
        raise RepairRefused("RECOVERY_BOUND_EXCEEDED")
    statuses = _statuses(client, ids)
    groups = []
    total = 0
    for automation_id in ids:
        status = statuses[automation_id]
        cached = db.execute("SELECT 1 FROM cached_automations WHERE automation_id = ?", (automation_id,)).fetchone()
        if cached is None:
            raise RepairRefused("CACHE_MISSING")
        rows = db.execute(
            "SELECT * FROM fact_outbox WHERE automation_id = ? ORDER BY sequence_number LIMIT ?",
            (automation_id, max_facts + 1),
        ).fetchall()
        total += len(rows)
        if total > max_facts:
            raise RepairRefused("RECOVERY_BOUND_EXCEEDED")
        previous = journal.get(automation_id)
        first = previous["first_sequence"] if previous else rows[0]["sequence_number"]
        last = previous["last_sequence"] if previous else rows[-1]["sequence_number"]
        if rows and (
            len(rows) != last - first + 1
            or any(row["sequence_number"] != first + index for index, row in enumerate(rows))
        ):
            raise RepairRefused("QUEUE_SEQUENCE_CONFLICT")
        if previous and not rows and status.last_sequence_number < last:
            raise RepairRefused("JOURNAL_ACK_CONFLICT")
        new_rows = [row for row in rows if row["sequence_number"] > status.last_sequence_number]
        if new_rows and new_rows[0]["sequence_number"] != status.last_sequence_number + 1:
            raise RepairRefused("CORE_SEQUENCE_GAP")
        corrections = {}
        facts = []
        for row in rows:
            if row["delivery_state"] not in {"FAILED", "PENDING"}:
                raise RepairRefused("UNEXPECTED_DELIVERY_STATE")
            if row["fact_kind"] == "AUTOMATION_STATE_CHANGED":
                raise RepairRefused("AUTOMATION_STATE_FACT_PRESENT")
            if row["user_broker_id"] != str(status.user_broker_id):
                raise RepairRefused("CORE_SCOPE_CONFLICT")
            accepted = row["sequence_number"] <= status.last_sequence_number
            if accepted and previous is None:
                raise RepairRefused("ACCEPTED_FACT_CONFLICT")
            if not accepted and row["expected_revision"] != status.revision:
                raise RepairRefused("CORE_REVISION_CONFLICT")
            payload = json.loads(row["payload"])
            if row["fact_kind"] == STATE_FACT:
                snapshot = payload.get("strategy_snapshot")
                if not isinstance(snapshot, dict):
                    raise RepairRefused("SNAPSHOT_INVALID")
                canonical = db.execute(
                    "SELECT strategy_snapshot FROM trade_decisions WHERE id = ? AND automation_id = ?",
                    (payload.get("decision_id"), automation_id),
                ).fetchone()
                cleaned = {key: value for key, value in snapshot.items() if key != PRIVATE_KEY}
                if canonical is None or cleaned != json.loads(canonical[0]):
                    raise RepairRefused("DECISION_SNAPSHOT_CONFLICT")
                if PRIVATE_KEY in snapshot:
                    if accepted or previous is not None:
                        raise RepairRefused("ACCEPTED_OR_JOURNALED_METADATA_CONFLICT")
                    payload["strategy_snapshot"] = cleaned
                    corrections[row["event_id"]] = payload
            envelope = {
                key: row[key]
                for key in (
                    "event_id",
                    "user_broker_id",
                    "automation_id",
                    "sequence_number",
                    "expected_revision",
                    "fact_kind",
                    "safe_message",
                )
            }
            # UTCDateTime stores naive UTC in SQLite; restore its declared zone.
            # Leave the millisecond validator and all payload timestamps untouched.
            occurred_at = datetime.fromisoformat(row["occurred_at"])
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            envelope["occurred_at"] = occurred_at
            envelope["payload"] = payload
            facts.append(ADAPTER.validate_python(envelope))
        groups.append(Group(automation_id, rows, facts, corrections, first, last))
    return groups


def repair_database(
    database: Path,
    client: CoreClient,
    *,
    apply: bool = False,
    max_facts: int = 10_000,
) -> dict[str, int]:
    """Validate all groups before mutation; publish each group atomically in Core.

    A transport failure intentionally leaves the corrected queue and journal for
    explicit rerun. Duplicate facts must receive an exact event-ID ACK before any
    local deletion. The SQLite write lock prevents a Worker start during each
    transaction, and clean_shutdown is rechecked after every commit.
    """
    if max_facts < 1:
        raise RepairRefused("INVALID_RECOVERY_BOUND")
    path = Path(database).resolve(strict=True)
    db = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if apply else 'ro'}", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        groups = _plan(db, client, max_facts)
        result = {
            "groups": len(groups),
            "corrections": sum(len(group.corrections) for group in groups),
            "queued_facts": sum(len(group.facts) for group in groups),
            "acknowledged_facts": 0,
            "reconciled_groups": 0,
        }
        if not apply or not groups:
            db.rollback()
            return result
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {JOURNAL} (automation_id TEXT PRIMARY KEY, "
            "first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL)"
        )
        for group in groups:
            db.execute(
                f"INSERT OR IGNORE INTO {JOURNAL} VALUES (?, ?, ?)",
                (group.automation_id, group.first_sequence, group.last_sequence),
            )
            for event_id, payload in group.corrections.items():
                db.execute("UPDATE fact_outbox SET payload = ? WHERE event_id = ?", (json.dumps(payload), event_id))
            db.execute(
                "UPDATE fact_outbox SET delivery_state = 'PENDING', next_retry_at = NULL "
                "WHERE automation_id = ? AND delivery_state = 'FAILED'",
                (group.automation_id,),
            )
        db.commit()
        for group in groups:
            db.execute("BEGIN IMMEDIATE")
            _stopped(db)
            if group.facts:
                response = client.publish_facts(group.facts)
                if response.failures or len(response.results) != 1:
                    raise RepairRefused("CORE_PUBLISH_REJECTED")
                ack = response.results[0]
                expected_ids = {fact.event_id for fact in group.facts}
                if (
                    str(ack.automation_id) != group.automation_id
                    or set(ack.accepted_event_ids) != expected_ids
                    or len(ack.accepted_event_ids) != len(expected_ids)
                    or ack.accepted_through_sequence < group.last_sequence
                ):
                    raise RepairRefused("CORE_ACK_CONFLICT")
                # Delete exactly the acknowledged facts, following Worker outbox semantics.
                db.executemany("DELETE FROM fact_outbox WHERE event_id = ?", [(str(value),) for value in expected_ids])
                result["acknowledged_facts"] += len(expected_ids)
            current = _statuses(client, [group.automation_id])[group.automation_id]
            if current.last_sequence_number < group.last_sequence:
                raise RepairRefused("CORE_RECONCILIATION_CONFLICT")
            db.execute(
                "UPDATE cached_automations SET state = ?, revision = ?, last_sequence_number = ?, "
                "resume_requested = ? WHERE automation_id = ?",
                (
                    current.state.value,
                    current.revision,
                    current.last_sequence_number,
                    current.resume_requested,
                    group.automation_id,
                ),
            )
            db.execute(f"DELETE FROM {JOURNAL} WHERE automation_id = ?", (group.automation_id,))
            result["reconciled_groups"] += 1
            if db.execute(f"SELECT COUNT(*) FROM {JOURNAL}").fetchone()[0] == 0:
                db.execute(f"DROP TABLE {JOURNAL}")
            db.commit()
        return result
    finally:
        db.close()


def main(argv: list[str] | None = None, *, output: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-facts", type=int, default=10_000)
    args = parser.parse_args(argv)
    output = output or sys.stdout
    try:
        with httpx.Client(base_url=args.core_url, timeout=30, trust_env=False) as http:
            result = repair_database(args.database, CoreClient(http), apply=args.apply, max_facts=args.max_facts)
    except Exception as error:
        # Never print exception messages, URLs, SQL parameters or fact payloads.
        code = str(error) if isinstance(error, RepairRefused) else type(error).__name__
        print(json.dumps({"error": code}), file=output)
        return 1
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
