"""One-off, explicitly fenced TEST purge. No application imports or remote actions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager

from sqlalchemy import MetaData, and_, case, create_engine, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError


class PurgeError(RuntimeError):
    """Safe stable error code; never include database exception text."""


# Frozen public catalog captured 2026-10-11; CHECK/index migrations are independent.
COLUMNS = {
    "alembic_version": ("version_num"),
    "automation_events": (
        "event_id automation_id user_broker_id sequence_number expected_revision fact_kind safe_message "
        "payload occurred_at received_at"
    ),
    "broker_account_fee_profiles": (
        "user_broker_id instrument_type currency buy_rate sell_rate service_rate deal_rate source "
        "calculated_at valid_until id created_at updated_at"
    ),
    "broker_instruments": (
        "user_broker_id external_instrument_id external_identifiers ticker name instrument_type "
        "class_code currency lot_size min_price_increment api_trade_available is_active is_selected "
        "first_seen_at last_seen_at id created_at updated_at"
    ),
    "broker_order_events": (
        "fact_id user_broker_id automation_id broker_order_id from_state to_state safe_reason "
        "safe_message occurred_at created_at id"
    ),
    "broker_orders": (
        "fact_id user_broker_id automation_id decision_id position_cycle_id instrument_id "
        "idempotency_key external_order_id intent_kind side order_type state quantity_lots limit_price "
        "requested_amount executed_amount estimated_commission executed_commission strategy_snapshot "
        "dispatch_started_at broker_responded_at executed_at terminal_at id created_at updated_at"
    ),
    "execution_lot_allocations": (
        "user_broker_id automation_id position_cycle_id sell_execution_id position_lot_id allocated_lots "
        "entry_value exit_value entry_commission exit_commission realized_pnl allocated_at created_at id"
    ),
    "instrument_sync_state": (
        "user_broker_id status last_attempt_at last_success_at safe_error reconciliation_required "
        "created_at updated_at"
    ),
    "portfolio_snapshot_runs": ("captured_at bucket_start safe_errors created_at id"),
    "portfolio_snapshots": (
        "run_id user_broker_id account_id total_value free_cash cumulative_pnl currency captured_at "
        "bucket_start created_at id"
    ),
    "position_cycles": (
        "user_broker_id automation_id instrument_id state quantity_lots average_entry_price "
        "invested_amount realized_pnl unrealized_pnl net_pnl accumulated_commissions opened_at closed_at "
        "id created_at updated_at"
    ),
    "position_lots": (
        "user_broker_id automation_id position_cycle_id buy_execution_id source original_lots "
        "remaining_lots entry_price entry_commission opened_at id created_at updated_at"
    ),
    "position_valuation_snapshots": (
        "user_broker_id automation_id position_cycle_id instrument_id quantity_lots average_price "
        "current_price invested_amount market_value realized_pnl unrealized_pnl net_pnl "
        "actual_commissions source captured_at created_at id"
    ),
    "trade_audit_events": (
        "event_id process_id parent_process_id user_broker_id automation_id decision_id broker_order_id "
        "execution_id instrument_id level stage safe_message data occurred_at created_at critical"
    ),
    "trade_decisions": (
        "fact_id process_id user_broker_id automation_id position_cycle_id instrument_id quantity_lots "
        "lot_size average_price invested_amount current_price best_bid best_ask indicators "
        "estimated_commission decision reason_code requested_quantity_lots limit_price strategy_snapshot "
        "decided_at created_at id"
    ),
    "trade_executions": (
        "fact_id user_broker_id automation_id broker_order_id position_cycle_id instrument_id "
        "external_execution_id side executed_lots price value broker_commission other_fees currency "
        "source executed_at created_at id"
    ),
    "trading_account_admissions": (
        "user_broker_id revision owner_runtime_instance_id owner_generation recovery_runtime_instance_id "
        "admission_status recovery_only lease_expires_at arm_revision arm_expires_at limits "
        "configuration_fingerprint baseline_id reconciliation_id created_at updated_at"
    ),
    "trading_admission_baselines": (
        "baseline_id user_broker_id account_id moscow_day currency equity net_external_cash_flow "
        "capture_started_at capture_finished_at operations_through accepted_by accepted_at payload"
    ),
    "trading_admission_events": (
        "event_id user_broker_id runtime_instance_id generation event_type actor occurred_at payload"
    ),
    "trading_admission_reconciliations": (
        "reconciliation_id user_broker_id account_id scope owner_runtime_instance_id owner_generation "
        "configuration_fingerprint status canary_uid captured_at valid_until can_arm "
        "position_anchor_units active_ordinary_order_count active_stop_order_count "
        "unresolved_reservation_count reasons risk_snapshot_id payload"
    ),
    "trading_automations": (
        "user_broker_id instrument_id state suspended_from_state hold_reason revision "
        "last_sequence_number resume_requested closed_at bootstrap_position_cycle_id "
        "bootstrap_position_lot_id bootstrap_quantity_lots bootstrap_average_price "
        "bootstrap_invested_amount bootstrap_currency bootstrap_observed_at id created_at updated_at "
        "settings_revision order_quota_percent"
    ),
    "trading_submit_reservations": (
        "reservation_id user_broker_id automation_id idempotency_key request_fingerprint state "
        "runtime_instance_id generation configuration_fingerprint permit_id side instrument_uid "
        "quantity_lots limit_price requested_amount reserved_cash reserved_position_units payload "
        "created_at updated_at"
    ),
    "user_brokers": (
        "api_slug display_name environment fqdn settings external_account_id state id created_at "
        "updated_at archived_at"
    ),
    "worker_runtime_instances": (
        "runtime_instance_id user_broker_id worker_id environment access_mode strategy_enabled "
        "secret_digest registration_status generation registered_at last_seen_at lease_expires_at "
        "retired_at"
    ),
}
PRIMARY_KEYS = {
    "alembic_version": "version_num",
    "automation_events": "event_id",
    "broker_account_fee_profiles": "id",
    "broker_instruments": "id",
    "broker_order_events": "id",
    "broker_orders": "id",
    "execution_lot_allocations": "id",
    "instrument_sync_state": "user_broker_id",
    "portfolio_snapshot_runs": "id",
    "portfolio_snapshots": "id",
    "position_cycles": "id",
    "position_lots": "id",
    "position_valuation_snapshots": "id",
    "trade_audit_events": "event_id",
    "trade_decisions": "id",
    "trade_executions": "id",
    "trading_account_admissions": "user_broker_id",
    "trading_admission_baselines": "baseline_id",
    "trading_admission_events": "event_id",
    "trading_admission_reconciliations": "reconciliation_id",
    "trading_automations": "id",
    "trading_submit_reservations": "reservation_id",
    "user_brokers": "id",
    "worker_runtime_instances": "runtime_instance_id",
}
FOREIGN_KEYS = {
    "alembic_version": [],
    "automation_events": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT")
    ],
    "broker_account_fee_profiles": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "broker_instruments": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "broker_order_events": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "broker_order_id"), "broker_orders", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "broker_orders": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "decision_id"), "trade_decisions", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "execution_lot_allocations": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_lot_id"), "position_lots", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "sell_execution_id"), "trade_executions", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "instrument_sync_state": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "portfolio_snapshot_runs": [],
    "portfolio_snapshots": [
        (("run_id",), "portfolio_snapshot_runs", ("id",), "CASCADE"),
        (("user_broker_id",), "user_brokers", ("id",), "RESTRICT"),
    ],
    "position_cycles": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "position_lots": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "buy_execution_id"), "trade_executions", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "position_valuation_snapshots": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "trade_audit_events": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "decision_id"), "trade_decisions", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "execution_id"), "trade_executions", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "broker_order_id"), "broker_orders", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "trade_decisions": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "trade_executions": [
        (("user_broker_id", "automation_id"), "trading_automations", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "position_cycle_id"), "position_cycles", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT"),
        (("user_broker_id", "broker_order_id"), "broker_orders", ("user_broker_id", "id"), "RESTRICT"),
    ],
    "trading_account_admissions": [
        (("recovery_runtime_instance_id",), "worker_runtime_instances", ("runtime_instance_id",), "RESTRICT"),
        (("user_broker_id",), "user_brokers", ("id",), "RESTRICT"),
        (
            ("user_broker_id", "owner_runtime_instance_id"),
            "worker_runtime_instances",
            ("user_broker_id", "runtime_instance_id"),
            "RESTRICT",
        ),
    ],
    "trading_admission_baselines": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "trading_admission_events": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "trading_admission_reconciliations": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "trading_automations": [
        (("user_broker_id", "instrument_id"), "broker_instruments", ("user_broker_id", "id"), "RESTRICT")
    ],
    "trading_submit_reservations": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
    "user_brokers": [],
    "worker_runtime_instances": [(("user_broker_id",), "user_brokers", ("id",), "RESTRICT")],
}
SCOPED = set(COLUMNS) - {"portfolio_snapshot_runs", "alembic_version", "user_brokers"}


def _catalog(connection):
    inspector = inspect(connection)
    if connection.dialect.name == "postgresql":
        unsafe = connection.scalar(
            text(
                """
            SELECT EXISTS (
              SELECT 1 FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
              WHERE n.nspname = current_schema() AND
                (t.relrowsecurity OR EXISTS (
                  SELECT 1 FROM pg_trigger g WHERE g.tgrelid = t.oid AND NOT g.tgisinternal))
              UNION ALL
              SELECT 1 FROM pg_constraint f
              JOIN pg_class parent ON parent.oid = f.confrelid
              JOIN pg_namespace pn ON pn.oid = parent.relnamespace
              JOIN pg_class child ON child.oid = f.conrelid
              JOIN pg_namespace cn ON cn.oid = child.relnamespace
              WHERE f.contype = 'f' AND pn.nspname = current_schema()
                AND cn.nspname <> current_schema()
            )
        """
            )
        )
    else:
        unsafe = connection.scalar(text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' LIMIT 1"))
    if unsafe:
        raise PurgeError("UNSUPPORTED_SCHEMA")
    present = set(inspector.get_table_names())
    if present not in (set(COLUMNS), set(COLUMNS) - {"alembic_version"}):
        raise PurgeError("UNSUPPORTED_SCHEMA")
    for name in present:
        if {c["name"] for c in inspector.get_columns(name)} != set(COLUMNS[name].split()):
            raise PurgeError("UNSUPPORTED_SCHEMA")
        if inspector.get_pk_constraint(name)["constrained_columns"] != [PRIMARY_KEYS[name]]:
            raise PurgeError("UNSUPPORTED_SCHEMA")
        foreign_keys = inspector.get_foreign_keys(name)
        if any(f.get("referred_schema") not in (None, inspector.default_schema_name) for f in foreign_keys):
            raise PurgeError("UNSUPPORTED_SCHEMA")
        actual = {
            (
                tuple(f["constrained_columns"]),
                f["referred_table"],
                tuple(f["referred_columns"]),
                f["options"].get("ondelete", "NO ACTION"),
            )
            for f in foreign_keys
        }
        if actual != set(FOREIGN_KEYS[name]):
            raise PurgeError("UNSUPPORTED_SCHEMA")
    metadata = MetaData()
    metadata.reflect(connection)
    return metadata


def _predicate(table, tables):
    scopes = tables["user_brokers"]
    if table.name == "user_brokers":
        return table.c.environment == "TEST"
    predicate = table.c.user_broker_id.in_(select(scopes.c.id).where(scopes.c.environment == "TEST"))
    if "environment" in table.c:
        predicate = and_(predicate, table.c.environment == "TEST")
    return predicate


def _preflight(connection, metadata):
    tables = metadata.tables
    runtime = tables["worker_runtime_instances"]
    if connection.scalar(
        select(runtime.c.runtime_instance_id)
        .where(
            runtime.c.user_broker_id.in_(
                select(tables["user_brokers"].c.id).where(tables["user_brokers"].c.environment == "TEST")
            ),
            runtime.c.environment.is_distinct_from("TEST"),
        )
        .limit(1)
    ):
        raise PurgeError("PROTECTED_REFERENCE")
    # Composite scoped FKs cannot cross environments; the unscoped recovery FK can.
    for child in metadata.tables.values():
        if child.name not in SCOPED:
            continue
        for fk in child.foreign_key_constraints:
            parent = next(iter(fk.elements)).column.table
            if parent.name == "portfolio_snapshot_runs" or "user_broker_id" in fk.column_keys:
                continue
            joins = and_(*(el.parent == el.column for el in fk.elements))
            if connection.scalar(
                select(1)
                .select_from(child.join(parent, joins))
                .where(_predicate(parent, tables), _predicate(child, tables).is_not(True))
                .limit(1)
            ):
                raise PurgeError("PROTECTED_REFERENCE")


@contextmanager
def _transaction(engine, *, readonly=False):
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            if readonly:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '60s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '60s'")
        yield connection


def _guard_scopes(connection, tables, scopes):
    table = tables["user_brokers"]
    rows = connection.execute(
        select(table.c.id, table.c.environment, table.c.state)
        .where((table.c.id.in_(scopes)) | (table.c.environment == "TEST"))
        .order_by(table.c.id)
        .with_for_update()
    ).all()
    if {r.id for r in rows} != scopes or any(r.environment != "TEST" or r.state != "DISABLED" for r in rows):
        raise PurgeError("SCOPE_DRIFT")


def _run_errors(errors, scopes):
    if not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors):
        raise PurgeError("UNSUPPORTED_RUN_ERRORS")
    return [e for e in errors if not isinstance(e.get("broker_id"), str) or e["broker_id"] not in scopes]


def _portfolio(run, scopes, counts):
    test_count, protected_count = counts.get(run.id, (0, 0))
    errors = _run_errors(run.safe_errors, scopes)
    removed_errors = len(run.safe_errors) - len(errors)
    pure = (test_count > 0 or removed_errors > 0) and protected_count == 0 and not errors
    return errors, removed_errors, pure


def _run_purge(engine, *, apply, writers_fenced, batch_size, progress):
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise PurgeError("UNSUPPORTED_DATABASE")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 10000:
        raise PurgeError("INVALID_BATCH_SIZE")
    if apply and not writers_fenced:
        raise PurgeError("WRITERS_NOT_FENCED")
    with _transaction(engine, readonly=True) as connection:
        metadata = _catalog(connection)
        tables = metadata.tables
        _preflight(connection, metadata)
        scopes = set(
            connection.scalars(
                select(tables["user_brokers"].c.id).where(tables["user_brokers"].c.environment == "TEST")
            )
        )
        counts = {
            name: (
                int(connection.scalar(select(func.count()).select_from(table).where(_predicate(table, tables))))
                if name in SCOPED | {"user_brokers"}
                else 0
            )
            for name, table in tables.items()
        }
        run_table = tables["portfolio_snapshot_runs"]
        snapshots = tables["portfolio_snapshots"]
        grouped = select(
            snapshots.c.run_id,
            func.sum(case((_predicate(snapshots, tables), 1), else_=0)),
            func.sum(case((_predicate(snapshots, tables), 0), else_=1)),
        ).group_by(snapshots.c.run_id)
        # One pass over snapshots; never rescan the full table for each global run.
        portfolio_counts = {row[0]: (row[1], row[2]) for row in connection.execute(grouped)}
        error_count = 0
        for run in connection.execute(select(run_table).order_by(run_table.c.id)):
            _, removed, pure = _portfolio(run, scopes, portfolio_counts)
            counts[run_table.name] += int(pure)
            error_count += removed
    report = {
        "mode": "apply" if apply else "dry-run",
        "candidates": counts,
        "deleted": dict.fromkeys(counts, 0),
        "test_errors": error_count,
        "removed_test_errors": 0,
    }
    if not apply or not scopes:
        return report
    with _transaction(engine) as connection:
        scope_table = tables["user_brokers"]
        rows = connection.execute(
            select(scope_table.c.id, scope_table.c.environment).where(scope_table.c.id.in_(scopes)).with_for_update()
        ).all()
        if {r.id for r in rows if r.environment == "TEST"} != scopes:
            raise PurgeError("SCOPE_DRIFT")
        _preflight(connection, metadata)
        connection.execute(
            scope_table.update()
            .where(scope_table.c.id.in_(scopes), scope_table.c.environment == "TEST")
            .values(state="DISABLED")
        )
    # Runs carry provenance: commit the last snapshot batch and its run/errors together.
    last_run = None
    while True:
        last_snapshot = None
        done_run = False
        while not done_run:
            with _transaction(engine) as connection:
                _guard_scopes(connection, tables, scopes)
                query = select(run_table).order_by(run_table.c.id).limit(1).with_for_update()
                if last_run is not None:
                    query = query.where(run_table.c.id > last_run)
                run = connection.execute(query).first()
                if run is None:
                    done_run = True
                    break
                errors, removed, pure = _portfolio(run, scopes, portfolio_counts)
                snapshots = tables["portfolio_snapshots"]
                query = (
                    select(snapshots.c.id)
                    .where(snapshots.c.run_id == run.id, _predicate(snapshots, tables))
                    .order_by(snapshots.c.id)
                    .limit(batch_size + 1)
                )
                if last_snapshot is not None:
                    query = query.where(snapshots.c.id > last_snapshot)
                ids = list(connection.scalars(query.with_for_update()))
                done_run = len(ids) <= batch_size
                ids = ids[:batch_size]
                if ids:
                    connection.execute(snapshots.delete().where(snapshots.c.id.in_(ids), _predicate(snapshots, tables)))
                    last_snapshot = ids[-1]
                if done_run:
                    if pure:
                        pure = bool(
                            connection.execute(
                                run_table.delete().where(
                                    run_table.c.id == run.id, ~select(1).where(snapshots.c.run_id == run.id).exists()
                                )
                            ).rowcount
                        )
                    if not pure and removed:
                        connection.execute(
                            run_table.update().where(run_table.c.id == run.id).values(safe_errors=errors)
                        )
            report["deleted"]["portfolio_snapshots"] += len(ids)
            if done_run:
                report["deleted"][run_table.name] += int(pure)
                report["removed_test_errors"] += removed
                last_run = run.id
            if progress:
                progress(report)
        if run is None:
            break
    for table in reversed(metadata.sorted_tables):
        if table.name not in SCOPED | {"user_brokers"} or table.name == "portfolio_snapshots":
            continue
        key = table.c[PRIMARY_KEYS[table.name]]
        cursor = None
        while True:
            with _transaction(engine) as connection:
                _guard_scopes(connection, tables, scopes)
                query = select(key).where(_predicate(table, tables)).order_by(key).limit(batch_size)
                if cursor is not None:
                    query = query.where(key > cursor)
                ids = list(connection.scalars(query))
                if not ids:
                    break
                deleted = connection.execute(table.delete().where(key.in_(ids), _predicate(table, tables))).rowcount
            report["deleted"][table.name] += deleted
            cursor = ids[-1]
            if table.name == "user_brokers":
                scopes.difference_update(ids)
            if progress:
                progress(report)
    return report


def run_purge(engine, *, apply=False, writers_fenced=False, batch_size=2000, progress=None):
    """Return aggregate receipts; each committed batch can be safely resumed."""
    try:
        return _run_purge(engine, apply=apply, writers_fenced=writers_fenced, batch_size=batch_size, progress=progress)
    except SQLAlchemyError:
        raise PurgeError("DATABASE_OPERATION_FAILED") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-fenced", action="store_true")
    parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args(argv)
    engine = None
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise PurgeError("DATABASE_URL_MISSING")  # noqa: TRY301 - sanitized CLI error boundary
        engine = create_engine(url, hide_parameters=True, echo=False)
        # One receipt per 100 batches bounds output even for multi-million-row tables.
        batches = 0

        def progress(report):
            nonlocal batches
            batches += 1
            if batches % 100 == 0:
                print(json.dumps({"batches": batches, "deleted": report["deleted"]}), flush=True)  # noqa: T201

        report = run_purge(
            engine, apply=args.apply, writers_fenced=args.writers_fenced, batch_size=args.batch_size, progress=progress
        )
        print(json.dumps(report, sort_keys=True))  # noqa: T201 - CLI receipt
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, PurgeError) else "PURGE_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr)  # noqa: T201 - sanitized CLI error
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
