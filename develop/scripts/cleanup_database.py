"""Explicit, bounded retention cleanup for Core PostgreSQL and Worker SQLite."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import DateTime, Engine, bindparam, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

MAX_BATCH_SIZE = 10_000

CORE_TABLES = (
    "automation_events",
    "broker_account_fee_profiles",
    "broker_instruments",
    "broker_order_events",
    "broker_orders",
    "execution_lot_allocations",
    "instrument_sync_state",
    "portfolio_snapshot_runs",
    "portfolio_snapshots",
    "position_cycles",
    "position_lots",
    "position_valuation_snapshots",
    "trade_audit_events",
    "trade_decisions",
    "trade_executions",
    "trading_account_admissions",
    "trading_admission_baselines",
    "trading_admission_events",
    "trading_admission_reconciliations",
    "trading_automations",
    "trading_submit_reservations",
    "user_brokers",
    "worker_runtime_instances",
)

WORKER_TABLES = (
    "account_commission_profiles",
    "broker_intents",
    "business_audit_events",
    "cached_automations",
    "fact_outbox",
    "lot_allocations",
    "trade_decisions",
    "trade_lots",
    "trading_cycle_states",
    "worker_runs",
)

CORE_REQUIRED_COLUMNS = {
    "trade_audit_events": {"event_id", "occurred_at", "critical", "user_broker_id", "automation_id"},
    "broker_orders": {"user_broker_id", "automation_id", "state", "terminal_at"},
    "trading_submit_reservations": {"user_broker_id", "automation_id", "state"},
    "position_valuation_snapshots": {"id", "captured_at", "user_broker_id", "position_cycle_id"},
    "portfolio_snapshots": {"id", "captured_at", "user_broker_id", "account_id", "currency"},
}

WORKER_REQUIRED_COLUMNS = {
    "business_audit_events": {"event_id", "automation_id", "occurred_at", "critical"},
    "broker_intents": {"automation_id", "state", "terminal_at", "execution_facts_emitted_at"},
    "fact_outbox": {"automation_id", "delivery_state"},
}

CORE_KEEP_REASONS = dict.fromkeys(
    CORE_TABLES,
    "Retained: current, referenced, idempotency, execution, or financial history.",
)
CORE_KEEP_REASONS.update(
    {
        "trade_audit_events": (
            "Noncritical events older than cutoff are eligible only when their automation has no "
            "nonterminal/incompletely finalized order or active submit reservation. Critical and recent rows stay."
        ),
        "position_valuation_snapshots": (
            "Only snapshots older than cutoff that are not latest for their broker scope and position cycle "
            "are eligible; "
            "all ties at the latest timestamp stay."
        ),
        "portfolio_snapshots": (
            "Only snapshots older than cutoff that are not latest for their broker/account/currency are eligible; "
            "all ties at the latest timestamp stay. Portfolio run rows remain as bucket/idempotency anchors."
        ),
    }
)

WORKER_KEEP_REASONS = dict.fromkeys(
    WORKER_TABLES,
    "Retained: current execution/recovery state, durable identity, commission configuration, or outbox.",
)
WORKER_KEEP_REASONS["business_audit_events"] = (
    "Only noncritical rows older than cutoff are eligible; protect every automation with an active/unfinalized intent "
    "or PENDING/FAILED fact outbox row."
)

_CORE_PREDICATES = {
    "trade_audit_events": (
        "target.occurred_at < :cutoff AND target.critical IS FALSE "
        "AND NOT EXISTS (SELECT 1 FROM broker_orders AS orders "
        "WHERE orders.user_broker_id = target.user_broker_id "
        "AND orders.automation_id = target.automation_id "
        "AND (orders.state NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED') "
        "OR orders.terminal_at IS NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM trading_submit_reservations AS reservations "
        "WHERE reservations.user_broker_id = target.user_broker_id "
        "AND reservations.automation_id = target.automation_id "
        "AND reservations.state IN ('IN_FLIGHT', 'BROKER_ACTIVE', 'UNKNOWN'))"
    ),
    "position_valuation_snapshots": (
        "target.captured_at < :cutoff "
        "AND EXISTS (SELECT 1 FROM position_valuation_snapshots AS newer "
        "WHERE newer.user_broker_id = target.user_broker_id "
        "AND newer.position_cycle_id = target.position_cycle_id "
        "AND newer.captured_at > target.captured_at)"
    ),
    "portfolio_snapshots": (
        "target.captured_at < :cutoff "
        "AND EXISTS (SELECT 1 FROM portfolio_snapshots AS newer "
        "WHERE newer.user_broker_id = target.user_broker_id "
        "AND newer.account_id = target.account_id AND newer.currency = target.currency "
        "AND newer.captured_at > target.captured_at)"
    ),
}

_PRIMARY_KEYS = {
    "trade_audit_events": "event_id",
    "position_valuation_snapshots": "id",
    "portfolio_snapshots": "id",
    "business_audit_events": "event_id",
}

_EXPECTED_PRIMARY_KEYS = {
    "trade_audit_events": ("event_id",),
    "position_valuation_snapshots": ("id",),
    "portfolio_snapshots": ("id",),
    "business_audit_events": ("event_id",),
}

_TIMESTAMPS = {
    "trade_audit_events": "occurred_at",
    "position_valuation_snapshots": "captured_at",
    "portfolio_snapshots": "captured_at",
    "business_audit_events": "occurred_at",
}

_WORKER_AUDIT_PREDICATE = (
    "target.occurred_at < :cutoff AND target.critical IS FALSE "
    "AND NOT EXISTS (SELECT 1 FROM broker_intents AS intents "
    "WHERE intents.automation_id = target.automation_id AND ("
    "intents.state NOT IN ('FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED') "
    "OR intents.terminal_at IS NULL "
    "OR (intents.state = 'FILLED' AND intents.execution_facts_emitted_at IS NULL))) "
    "AND NOT EXISTS (SELECT 1 FROM fact_outbox AS outbox "
    "WHERE outbox.automation_id = target.automation_id "
    "AND outbox.delivery_state IN ('PENDING', 'FAILED'))"
)


def _calendar_month_before(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    utc_value = value.astimezone(UTC)
    year = utc_value.year
    month = utc_value.month - 1
    if month == 0:
        year -= 1
        month = 12
    day = min(utc_value.day, calendar.monthrange(year, month)[1])
    return utc_value.replace(year=year, month=month, day=day)


def _validate_batch_size(batch_size: int) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


def _validate_store_engine(engine: Engine, store: str) -> tuple[str, ...]:
    if store == "core":
        if engine.dialect.name != "postgresql":
            raise ValueError("core cleanup requires PostgreSQL")
        return CORE_TABLES
    if store == "worker":
        if engine.dialect.name != "sqlite":
            raise ValueError("worker cleanup requires SQLite")
        database = engine.url.database
        if not database or database == ":memory:":
            raise FileNotFoundError("worker SQLite file does not exist")
        database_path = Path(unquote(urlsplit(database).path)) if database.startswith("file:") else Path(database)
        if not database_path.is_file():
            raise FileNotFoundError("worker SQLite file does not exist")
        return WORKER_TABLES
    raise ValueError("store must be 'core' or 'worker'")


def _validate_schema(engine: Engine, store: str) -> None:
    expected_tables = _validate_store_engine(engine, store)
    required = CORE_REQUIRED_COLUMNS if store == "core" else WORKER_REQUIRED_COLUMNS
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if store == "worker":
                connection.exec_driver_sql("PRAGMA query_only = ON")
            if store == "core":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            inspector = inspect(connection)
            present_tables = set(inspector.get_table_names())
            missing_tables = set(expected_tables) - present_tables
            if missing_tables:
                raise ValueError("database schema is missing required tables")
            for table, required_columns in required.items():
                present_columns = {column["name"] for column in inspector.get_columns(table)}
                if not required_columns <= present_columns:
                    raise ValueError("database schema is missing required columns")
            for table, expected_primary_key in _EXPECTED_PRIMARY_KEYS.items():
                if table in expected_tables:
                    actual_primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"] or ())
                    if actual_primary_key != expected_primary_key:
                        raise ValueError("database schema has an unexpected primary key")
        finally:
            transaction.rollback()
            if store == "worker":
                connection.exec_driver_sql("PRAGMA query_only = OFF")


def _parameters(cutoff: datetime) -> dict[str, datetime]:
    # Keep UTC awareness so PostgreSQL never interprets the boundary in the session timezone.
    return {"cutoff": cutoff.astimezone(UTC)}


def _candidate_count(connection: Any, table: str, predicate: str, parameters: dict[str, datetime]) -> int:
    # Names and predicates are selected only from the module's fixed allowlists.
    statement = text(f"SELECT count(*) FROM {table} AS target WHERE {predicate}").bindparams(  # noqa: S608
        bindparam("cutoff", type_=DateTime(timezone=True))
    )  # noqa: S608
    return int(connection.scalar(statement, parameters) or 0)


def _delete_one_batch(
    connection: Any,
    *,
    table: str,
    predicate: str,
    primary_key: str,
    timestamp: str,
    parameters: dict[str, datetime],
    batch_size: int,
    postgres: bool,
) -> int:
    if postgres:
        statement = text(  # noqa: S608
            f"WITH targets AS (SELECT target.{primary_key} FROM {table} AS target "  # noqa: S608
            f"WHERE {predicate} ORDER BY target.{timestamp}, target.{primary_key} "
            "LIMIT :batch_size FOR UPDATE SKIP LOCKED) "
            f"DELETE FROM {table} AS target USING targets "
            f"WHERE target.{primary_key} = targets.{primary_key} RETURNING target.{primary_key}"
        ).bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))
    else:
        statement = text(  # noqa: S608
            f"DELETE FROM {table} WHERE {primary_key} IN ("  # noqa: S608
            f"SELECT target.{primary_key} FROM {table} AS target WHERE {predicate} "
            f"ORDER BY target.{timestamp}, target.{primary_key} LIMIT :batch_size)"
        ).bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))
    result = connection.execute(statement, {**parameters, "batch_size": batch_size})
    if postgres:
        return len(result.fetchall())
    return max(result.rowcount or 0, 0)


def _delete_batches(
    engine: Engine,
    *,
    store: str,
    table: str,
    predicate: str,
    parameters: dict[str, datetime],
    batch_size: int,
) -> int:
    deleted = 0
    while True:
        with engine.connect() as connection:
            if store == "worker":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            count = _delete_one_batch(
                connection,
                table=table,
                predicate=predicate,
                primary_key=_PRIMARY_KEYS[table],
                timestamp=_TIMESTAMPS[table],
                parameters=parameters,
                batch_size=batch_size,
                postgres=store == "core",
            )
            connection.commit()
        deleted += count
        if count == 0:
            return deleted


def run_cleanup(
    engine: Engine,
    store: str,
    *,
    now: datetime | None = None,
    apply: bool = False,
    batch_size: int = 1000,
) -> dict[str, object]:
    """Count eligible rows or delete in bounded transactions.

    Each batch commits independently; if a later batch fails, earlier batches may already be committed.
    A fresh dry-run recount is required after any failure.
    """
    _validate_batch_size(batch_size)
    tables = _validate_store_engine(engine, store)
    moment = now or datetime.now(UTC)
    cutoff = _calendar_month_before(moment)
    predicates = _CORE_PREDICATES if store == "core" else {"business_audit_events": _WORKER_AUDIT_PREDICATE}
    reasons = CORE_KEEP_REASONS if store == "core" else WORKER_KEEP_REASONS

    _validate_schema(engine, store)
    initial_counts: dict[str, int] = {}
    parameters = _parameters(cutoff)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if store == "worker":
                connection.exec_driver_sql("PRAGMA query_only = ON")
            if store == "core":
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for table, predicate in predicates.items():
                initial_counts[table] = _candidate_count(connection, table, predicate, parameters)
        finally:
            transaction.rollback()
            if store == "worker":
                connection.exec_driver_sql("PRAGMA query_only = OFF")

    deleted_counts = dict.fromkeys(predicates, 0)
    if apply:
        # Prune diagnostics before derived snapshots so the operator sees the highest-value space recovery first.
        for table, predicate in predicates.items():
            if initial_counts.get(table, 0):
                deleted_counts[table] = _delete_batches(
                    engine,
                    store=store,
                    table=table,
                    predicate=predicate,
                    parameters=parameters,
                    batch_size=batch_size,
                )

    report_tables: dict[str, dict[str, object]] = {}
    for table in tables:
        would_delete = initial_counts.get(table, 0)
        report_tables[table] = {
            "would_delete": would_delete,
            "deleted": deleted_counts.get(table, 0),
            "preserved_reason": reasons[table],
        }
    return {
        "store": store,
        "mode": "apply" if apply else "dry-run",
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "batch_size": batch_size,
        "tables": report_tables,
    }


def _sqlite_cli_url(raw_url: str, *, apply: bool) -> URL:
    url = make_url(raw_url)
    if url.drivername != "sqlite" and not url.drivername.startswith("sqlite+"):
        raise ValueError("worker URL must use SQLite")
    database = url.database
    if not database or database == ":memory:":
        raise ValueError("worker URL must point to an existing SQLite file")
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError("worker SQLite file does not exist")
    return url.set(
        database=path.as_uri(),
        query={**url.query, "mode": "rw" if apply else "ro", "uri": "true"},
    )


def _engine_from_environment(store: str, *, apply: bool) -> Engine:
    variable = "DATABASE_URL" if store == "core" else "AUTOMATON_DATABASE_URL"
    raw_url = os.environ.get(variable)
    if not raw_url:
        raise ValueError(f"required environment variable {variable} is missing")
    url = make_url(raw_url)
    if store == "core":
        if not url.drivername.startswith("postgresql"):
            raise ValueError("DATABASE_URL must use PostgreSQL")
    else:
        url = _sqlite_cli_url(raw_url, apply=apply)
    return create_engine(url, future=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or explicitly apply one-month database retention.")
    parser.add_argument("--store", choices=("core", "worker"), required=True)
    parser.add_argument("--apply", action="store_true", help="Delete eligible rows; default is dry-run.")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args(argv)
    engine: Engine | None = None
    try:
        _validate_batch_size(args.batch_size)
        engine = _engine_from_environment(args.store, apply=args.apply)
        report = run_cleanup(engine, args.store, apply=args.apply, batch_size=args.batch_size)
        sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except Exception as error:
        # Database/driver exception strings can contain DSNs or bound payloads; never print them.
        sys.stderr.write(f"Database cleanup failed ({type(error).__name__}); details withheld.\n")
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
