"""Bounded run_id comparison on the dedicated local PostgreSQL, never the live DB."""

# ruff: noqa: S101, T201

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, insert, schema, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.models import PortfolioSnapshotModel, PortfolioSnapshotRunModel, UserBrokerModel
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository

ROOT = Path(__file__).resolve().parents[2]
INDEX = "ix_portfolio_snapshots_run_id"
RUNS = 600
SCOPES = 8
ACCOUNTS = 3
CURRENCIES = ("EUR", "RUB", "USD")
NOW = datetime(2026, 10, 11, tzinfo=UTC)


def local_test_url(value):
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("Only the dedicated local test PostgreSQL is allowed") from None
    if url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1" or url.port != 55433 or url.query:
        raise ValueError("Only the dedicated local test PostgreSQL is allowed")
    return url


@contextmanager
def isolated_database(value):
    url = local_test_url(value)
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    owned = f"portfolio_index_{uuid4().hex}"
    created = False
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(schema.CreateSchema(owned))
        created = True
        isolated = url.update_query_dict({"options": f"-csearch_path={owned} -cstatement_timeout=30000"})
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", isolated.render_as_string(hide_password=False).replace("%", "%%"))
        command.upgrade(config, "0006_strict_state_invariants")
        engine = create_engine(isolated)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(schema.DropSchema(owned, cascade=True))
        finally:
            admin.dispose()


def identifier(number):
    return str(UUID(int=number))


def seed_brokers(engine):
    with engine.begin() as connection:
        connection.execute(
            insert(UserBrokerModel),
            [
                {
                    "id": identifier(scope + 1),
                    "api_slug": "synthetic",
                    "display_name": f"Synthetic {scope}",
                    "environment": "TEST" if scope % 2 else "PROD",
                    "fqdn": "synthetic.invalid",
                    "settings": {},
                    "external_account_id": f"synthetic-{scope}",
                    "state": "DISABLED",
                    "archived_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for scope in range(SCOPES)
            ],
        )


def seed_runs(connection, first, count):
    runs = []
    snapshots = []
    for number in range(first, first + count):
        bucket = NOW + timedelta(minutes=number)
        captured = NOW + timedelta(minutes=RUNS - 1 if number == RUNS - 2 else number)
        run_id = identifier(1000 + number)
        runs.append(
            {"id": run_id, "captured_at": captured, "bucket_start": bucket, "safe_errors": [], "created_at": captured}
        )
        for scope in range(SCOPES):
            for account in range(ACCOUNTS):
                for currency_index, currency in enumerate(CURRENCIES):
                    snapshots.append(
                        {
                            "id": identifier(
                                100000
                                + number * SCOPES * ACCOUNTS * len(CURRENCIES)
                                + scope * ACCOUNTS * len(CURRENCIES)
                                + account * len(CURRENCIES)
                                + currency_index
                            ),
                            "run_id": run_id,
                            "user_broker_id": identifier(scope + 1),
                            "account_id": f"synthetic-{scope}-{account}",
                            "currency": currency,
                            "total_value": 100000 + number,
                            "free_cash": 1000 + account,
                            "cumulative_pnl": number - 300,
                            "captured_at": captured,
                            "bucket_start": bucket,
                            "created_at": captured,
                        }
                    )
    connection.execute(insert(PortfolioSnapshotRunModel), runs)
    for start in range(0, len(snapshots), 1000):
        connection.execute(insert(PortfolioSnapshotModel), snapshots[start : start + 1000])


def repository_query(engine, environment, number):
    captured = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session:
            result = PortfolioSnapshotRepository(session, environment=environment).latest_snapshots(
                identifier(1000 + number)
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(captured) == 1
    expected = (
        [
            (currency, identifier(scope + 1), f"synthetic-{scope}-{account}")
            for currency in CURRENCIES
            for scope in range(SCOPES)
            if environment is None or ("TEST" if scope % 2 else "PROD") == environment
            for account in range(ACCOUNTS)
        ]
        if number < RUNS
        else []
    )
    assert [(row.currency, row.user_broker_id, row.account_id) for row in result] == expected
    assert all(row.run_id == identifier(1000 + number) for row in result)
    return captured[0], result


def measure_query(engine, environment, number):
    (sql, parameters), result = repository_query(engine, environment, number)
    samples = []
    with Session(engine) as session:
        repository = PortfolioSnapshotRepository(session, environment=environment)
        repository.latest_snapshots(identifier(1000 + number))
        for _ in range(15):
            started = perf_counter()
            assert repository.latest_snapshots(identifier(1000 + number)) == result
            samples.append((perf_counter() - started) * 1000)
    with engine.connect() as connection:
        explanation = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, parameters
        ).scalar_one()[0]
    plan = explanation["Plan"]
    return {
        "repository_median_ms": median(samples),
        "repository_samples_ms": samples,
        "execution_ms": explanation["Execution Time"],
        "shared_hit_blocks": plan.get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan.get("Shared Read Blocks", 0),
        "rows": len(result),
        "result_sha256": hashlib.sha256(
            json.dumps([row.model_dump(mode="json") for row in result], sort_keys=True).encode()
        ).hexdigest(),
        "sql": sql,
        "plan": plan,
    }, result


def measure_ingestion(engine):
    with engine.begin() as connection:
        start_lsn = connection.scalar(text("SELECT pg_current_wal_insert_lsn()"))
        started = perf_counter()
        seed_runs(connection, RUNS, 50)
    elapsed = (perf_counter() - started) * 1000
    with engine.connect() as connection:
        wal = int(
            connection.scalar(text("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), :start)"), {"start": start_lsn})
        )
    return {"runs": 50, "snapshots": 3600, "commit_included_ms": elapsed, "wal_bytes": wal}


def compare(value):
    cases = (
        ("latest_all", None, RUNS - 1),
        ("latest_test", "TEST", RUNS - 1),
        ("latest_prod", "PROD", RUNS - 1),
        ("latest_tie", None, RUNS - 2),
        ("history_test", "TEST", RUNS // 2),
        ("missing", None, RUNS + 100),
    )
    report = {
        "workload": {
            "runs": RUNS,
            "snapshots": RUNS * SCOPES * ACCOUNTS * len(CURRENCIES),
            "scopes": SCOPES,
            "accounts_per_scope": ACCOUNTS,
            "currencies": CURRENCIES,
            "latest_captured_at_ties": 2,
        },
        "phases": [],
    }
    originals = {}
    with isolated_database(value) as engine:
        with engine.connect() as connection:
            report["postgresql_version"] = connection.scalar(text("SHOW server_version"))
            assert int(connection.scalar(text("SHOW server_version_num"))) // 10000 == 16
        seed_brokers(engine)
        for indexed in (False, True, False, True):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE portfolio_snapshots, portfolio_snapshot_runs"))
                connection.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
                if indexed:
                    connection.execute(text(f"CREATE INDEX {INDEX} ON portfolio_snapshots (run_id)"))
                seed_runs(connection, 0, RUNS)
                connection.execute(text("ANALYZE portfolio_snapshots"))
                connection.execute(text("ANALYZE user_brokers"))
            phase = {"indexed": indexed, "queries": {}}
            for name, environment, number in cases:
                measured, result = measure_query(engine, environment, number)
                if name in originals:
                    assert originals[name] == result
                else:
                    originals[name] = result
                phase["queries"][name] = measured
            with engine.connect() as connection:
                phase["run_index_bytes"] = int(
                    connection.scalar(text("SELECT coalesce(pg_relation_size(to_regclass(:name)), 0)"), {"name": INDEX})
                )
            phase["ingestion"] = measure_ingestion(engine)
            report["phases"].append(phase)
    comparison = {}
    for name, _, _ in cases:
        baseline = [phase["queries"][name] for phase in report["phases"] if not phase["indexed"]]
        indexed = [phase["queries"][name] for phase in report["phases"] if phase["indexed"]]
        comparison[name] = {
            "latency_ratio": median(row["repository_median_ms"] for row in indexed)
            / median(row["repository_median_ms"] for row in baseline),
            "buffers_before": median(row["shared_hit_blocks"] + row["shared_read_blocks"] for row in baseline),
            "buffers_after": median(row["shared_hit_blocks"] + row["shared_read_blocks"] for row in indexed),
        }
    report["comparison"] = comparison
    report["benefit_proven"] = all(
        comparison[name]["latency_ratio"] < 1 and comparison[name]["buffers_after"] < comparison[name]["buffers_before"]
        for name in ("latest_all", "latest_test", "latest_prod")
    )
    report["semantics_preserved"] = True
    report["limitations"] = [
        "Synthetic bounded workload, warm cache; not production throughput or whole DB savings.",
        "WAL LSN delta is cluster-wide; use the dedicated test instance without concurrent writers.",
        "Insertion measurements include run/FK/existing-index work and commit; two samples per condition.",
        "BRIN not measured: exact run_id uses B-tree; time-index changes require a separate caller/workload.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare(os.environ["POSTGRES_TEST_DATABASE_URL"])
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except Exception as error:
        print(f"Portfolio index benchmark failed: {type(error).__name__}")
        return 1
    print(f"Portfolio run index benchmark: benefit_proven={report['benefit_proven']}, semantics_preserved=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
