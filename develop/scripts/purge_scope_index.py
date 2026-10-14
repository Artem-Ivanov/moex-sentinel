"""Temporary concurrent audit-scope index for completing the authorized TEST purge."""

import argparse
import json
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

INDEX = "ix_test_purge_audit_scope"
TABLE = "trade_audit_events"
EXPECTED_COLUMNS = {
    "event_id",
    "process_id",
    "parent_process_id",
    "user_broker_id",
    "automation_id",
    "decision_id",
    "broker_order_id",
    "execution_id",
    "instrument_id",
    "level",
    "stage",
    "safe_message",
    "data",
    "occurred_at",
    "created_at",
    "critical",
}


def manage_index(engine, *, apply=False, drop=False):
    if engine.dialect.name != "postgresql":
        raise ValueError("UNSUPPORTED_DATABASE")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("SET statement_timeout = '600s'")
        connection.exec_driver_sql("SET lock_timeout = '5s'")
        schema = connection.scalar(text("SELECT current_schema()"))
        if not schema or {c["name"] for c in inspect(connection).get_columns(TABLE, schema=schema)} != EXPECTED_COLUMNS:
            raise ValueError("UNSUPPORTED_AUDIT_SCHEMA")
        row = (
            connection.execute(
                text(
                    """
                SELECT t.relname AS table_name, am.amname AS method, a.attname AS column_name,
                       i.indnatts, i.indnkeyatts, i.indisunique, i.indisexclusion, i.indisvalid,
                       i.indpred IS NOT NULL AS partial, i.indexprs IS NOT NULL AS expression,
                       i.indoption[0] AS sort_options, i.indcollation[0] = a.attcollation AS default_collation,
                       op.opcdefault AS default_opclass
                FROM pg_class ix JOIN pg_namespace n ON n.oid = ix.relnamespace
                JOIN pg_index i ON i.indexrelid = ix.oid
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_am am ON am.oid = ix.relam
                JOIN pg_opclass op ON op.oid = i.indclass[0]
                LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
                WHERE n.nspname = :schema AND ix.relname = :index
            """
                ),
                {"schema": schema, "index": INDEX},
            )
            .mappings()
            .first()
        )
        if row and (
            row["table_name"] != TABLE
            or row["method"] != "btree"
            or row["column_name"] != "user_broker_id"
            or row["indnatts"] != 1
            or row["indnkeyatts"] != 1
            or row["indisunique"]
            or row["indisexclusion"]
            or row["partial"]
            or row["expression"]
            or row["sort_options"] != 0
            or not row["default_collation"]
            or not row["default_opclass"]
        ):
            raise ValueError("INDEX_NAME_CONFLICT")
        if row and not row["indisvalid"] and not drop:
            raise ValueError("INVALID_INDEX_REQUIRES_EXPLICIT_DROP")
        report = {
            "mode": "apply" if apply else "dry-run",
            "action": "drop" if drop else "create",
            "index": INDEX,
            "present_before": row is not None,
            "changed": False,
        }
        if apply and (bool(row) if drop else row is None):
            quote = connection.dialect.identifier_preparer.quote
            qualified_index = quote(schema) + "." + quote(INDEX)
            qualified_table = quote(schema) + "." + quote(TABLE)
            # Identifiers come only from fixed constants and quoted current_schema().
            ddl = (
                f"DROP INDEX CONCURRENTLY {qualified_index}"
                if drop
                else f"CREATE INDEX CONCURRENTLY {quote(INDEX)} ON {qualified_table} (user_broker_id)"
            )  # noqa: S608
            connection.exec_driver_sql(ddl)
            report["changed"] = True
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--drop", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    try:
        url = os.environ["DATABASE_URL"]
        engine = create_engine(url, echo=False, hide_parameters=True)
        print(  # noqa: T201 - CLI receipt.
            json.dumps(manage_index(engine, apply=args.apply, drop=args.drop), sort_keys=True)
        )
        return 0
    except (SQLAlchemyError, ValueError, KeyError):
        print(json.dumps({"error": "PURGE_INDEX_OPERATION_REFUSED"}))  # noqa: T201 - Sanitized CLI error.
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
