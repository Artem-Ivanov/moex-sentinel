"""Bounded synthetic C4 baseline profile; requires a root timing lease to run."""

# ruff: noqa: S101 -- Assertions are deliberate benchmark self-checks, never URL guards.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import statistics
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from uuid import uuid4

from sqlalchemy import Engine, event, func, insert, schema, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.services.trading_summary import TradingSummaryService
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.models.trading_analytics import PortfolioSnapshotModel, PortfolioSnapshotRunModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository

REPOSITORY = Path(__file__).resolve().parents[2]
REPORTS = REPOSITORY / "develop/reports/core-performance-20261004/c4"
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
BATCH_SIZE = 1000
MAX_HISTORY_ROWS = 86405
WARMUPS = 3
SAMPLES = 20
SCOPES = (None, "TEST", "PROD")
SOURCE_PATHS = {
    "reader": "src/moex_sentinel/storage/repositories/portfolio_snapshots.py",
    "service": "src/moex_sentinel/services/trading_summary.py",
}


@dataclass(frozen=True)
class Workload:
    name: str
    days: int
    minutes: int
    accounts: int
    snapshots: int

    @property
    def runs(self) -> int:
        return self.days * 24 * 60 // self.minutes + 1


WORKLOADS = (
    Workload("dense", 60, 60, 3, 4323),
    Workload("long", 180, 15, 4, 69124),
    Workload("fallback", 2, 60, 3, 147),
    Workload("composition", 60, 60, 3, 4321),
    Workload("mixed", 30, 60, 6, 4326),
)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def validate_database_url(value: str) -> URL:
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("Invalid C4 test database URL.") from None
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 60580
        or url.username != "sentinel_test"
        or url.database != "sentinel_core_performance_test"
        or url.query
    ):
        raise ValueError("Only the explicitly assigned local C4 test database is allowed.")
    return url


def validate_report_directory(value: Path) -> Path:
    destination = value.resolve()
    allowed = {REPORTS.resolve()}
    if REPOSITORY.parts[-4:] == ("develop", "reports", "core-performance-20261004", "worktree"):
        allowed.add((REPOSITORY.parent / "c4").resolve())
    if destination not in allowed:
        raise ValueError("Only the project C4 report directory or assigned outer worktree report directory is allowed.")
    return destination


def self_check() -> None:
    assert [(workload.runs, workload.snapshots) for workload in WORKLOADS] == [
        (1441, 4323),
        (17281, 69124),
        (49, 147),
        (1441, 4321),
        (721, 4326),
    ]
    assert max(workload.runs + workload.snapshots for workload in WORKLOADS) == MAX_HISTORY_ROWS
    assert nearest_rank_p95(list(range(1, 21))) == 19
    for unsafe in (
        "sqlite:///:memory:",
        "postgresql+psycopg://sentinel_test@localhost:60580/sentinel_core_performance_test",
        "postgresql+psycopg://sentinel_test@127.0.0.1:5432/sentinel_core_performance_test",
        "postgresql+psycopg://sentinel_test@127.0.0.1:60580/production",
    ):
        try:
            validate_database_url(unsafe)
        except ValueError:
            continue
        raise AssertionError("Unsafe URL accepted")


def freeze_sources(reports: Path = REPORTS) -> dict:
    reports.mkdir(parents=True, exist_ok=True)
    sources = {}
    for name, relative in SOURCE_PATHS.items():
        data = (REPOSITORY / relative).read_bytes()
        destination = reports / f"baseline_{name}.py"
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError("Refusing to overwrite a different frozen baseline source.")
        destination.write_bytes(data)
        sources[name] = {"original": relative, "frozen": destination.name, "sha256": digest_bytes(data)}
    result = {"sources": sources, "script_sha256": digest_bytes(Path(__file__).read_bytes()), "seed_phases": 2}
    write_json(reports / "source-freeze.json", result)
    return result


def load_sources(*, paired: bool = False, reports: Path = REPORTS):
    receipt = json.loads((reports / "source-freeze.json").read_text())
    if not paired and receipt["script_sha256"] != digest_bytes(Path(__file__).read_bytes()):
        raise ValueError("Profile script differs from the prepared source receipt.")
    modules = {}
    for name, source in receipt["sources"].items():
        path = reports / source["frozen"]
        if digest_bytes(path.read_bytes()) != source["sha256"]:
            raise ValueError("Frozen baseline source digest differs.")
        if not paired and digest_bytes((REPOSITORY / source["original"]).read_bytes()) != source["sha256"]:
            raise ValueError("Baseline phase requires unchanged C4 production sources.")
        module_name = f"c4_frozen_{name}"
        specification = importlib.util.spec_from_file_location(module_name, path)
        if specification is None or specification.loader is None:
            raise ValueError("Frozen source cannot be loaded.")
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        specification.loader.exec_module(module)
        modules[name] = module
    return modules["reader"].PortfolioSnapshotRepository, modules["service"].TradingSummaryService, receipt


def account_id(index: int) -> str:
    return f"account-{index}"


def broker_id(index: int) -> str:
    return f"broker-{index}"


def safe_errors() -> list[dict]:
    return [
        BrokerReadError(
            broker_id(index), "Synthetic", account_id(index), "BROKER_UNAVAILABLE", "Synthetic unavailable"
        ).model_dump(mode="json")
        for index in (0, 3)
    ]


def rows_for(workload: Workload, kind: str) -> Iterator[dict]:
    if kind == "brokers":
        count = 4 if workload.name == "composition" else workload.accounts
        for index in range(count):
            replaced = workload.name == "composition" and index == 2
            yield {
                "id": broker_id(index),
                "api_slug": "t_invest",
                "display_name": "Synthetic",
                "environment": "PROD" if workload.name == "mixed" and index >= 3 else "TEST",
                "fqdn": "synthetic.invalid",
                "settings": {},
                "external_account_id": account_id(index),
                "state": "DISABLED" if replaced else "ACTIVE",
                "created_at": NOW,
                "updated_at": NOW,
                "archived_at": NOW - timedelta(days=10) if replaced else None,
            }
        return
    first = NOW - timedelta(days=workload.days)
    for run_index in range(workload.runs):
        captured = first + timedelta(minutes=workload.minutes * run_index)
        run_id = f"{workload.name}-run-{run_index:06d}"
        if kind == "runs":
            yield {
                "id": run_id,
                "captured_at": captured,
                "bucket_start": captured,
                "created_at": captured,
                "safe_errors": safe_errors() if workload.name == "mixed" and captured == NOW else [],
            }
            continue
        for ordinal in range(workload.accounts):
            index = ordinal
            if workload.name == "composition" and ordinal == 2 and captured >= NOW - timedelta(days=10):
                index = 3
                if captured in (NOW - timedelta(days=1), NOW - timedelta(days=7)):
                    continue
            pnl = Decimal((run_index + 1) * (ordinal + 1)) / Decimal(10)
            yield {
                "id": f"{workload.name}-snapshot-{run_index:06d}-{ordinal}",
                "run_id": run_id,
                "user_broker_id": broker_id(index),
                "account_id": account_id(index),
                "currency": "USD" if workload.name == "mixed" and ordinal in (2, 5) else "RUB",
                "total_value": Decimal(1000) + pnl,
                "free_cash": Decimal(100),
                "cumulative_pnl": pnl,
                "captured_at": captured,
                "bucket_start": captured,
                "created_at": captured,
            }


def seed(engine: Engine, workload: Workload) -> dict:
    digest = hashlib.sha256()
    counts = {}
    with engine.begin() as connection:
        for kind, model in (
            ("brokers", UserBrokerModel),
            ("runs", PortfolioSnapshotRunModel),
            ("snapshots", PortfolioSnapshotModel),
        ):
            batch = []
            count = 0
            for row in rows_for(workload, kind):
                digest.update(
                    (kind + json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
                )
                batch.append(row)
                count += 1
                if len(batch) == BATCH_SIZE:
                    connection.execute(insert(model.__table__), batch)
                    batch.clear()
            if batch:
                connection.execute(insert(model.__table__), batch)
            counts[kind] = count
    with engine.connect() as connection:
        actual = {
            name: connection.scalar(select(func.count()).select_from(model))
            for name, model in (
                ("brokers", UserBrokerModel),
                ("runs", PortfolioSnapshotRunModel),
                ("snapshots", PortfolioSnapshotModel),
            )
        }
    assert actual == counts
    assert actual["runs"] == workload.runs
    assert actual["snapshots"] == workload.snapshots
    assert actual["runs"] + actual["snapshots"] <= MAX_HISTORY_ROWS
    return {"counts": actual, "digest": digest.hexdigest(), "batch_limit": BATCH_SIZE}


@contextmanager
def owned_engine(dialect: str, url: URL, run_directory: Path, workload: Workload):
    if dialect == "sqlite":
        path = run_directory / f"{workload.name}-{uuid4().hex}.sqlite3"
        engine = create_database_engine(f"sqlite:///{path}")
        try:
            Base.metadata.create_all(engine)
            yield engine, None
        finally:
            engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        return
    # The validated admin URL has no search_path. Only this generated schema is dropped.
    validate_database_url(url.render_as_string(hide_password=False))
    namespace = f"test_{uuid4().hex}"
    admin = create_database_engine(url)
    created = False
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(schema.CreateSchema(namespace))
        created = True
        isolated_url = url.update_query_dict({"options": f"-csearch_path={namespace}"})
        migration = Config(str(REPOSITORY / "alembic.ini"))
        migration.set_main_option("script_location", str(REPOSITORY / "alembic"))
        migration.set_main_option(
            "sqlalchemy.url", isolated_url.render_as_string(hide_password=False).replace("%", "%%")
        )
        command.upgrade(migration, "head")
        engine = create_database_engine(isolated_url)
        yield engine, namespace
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(schema.DropSchema(namespace, cascade=True))
        finally:
            admin.dispose()


@contextmanager
def capture_sql(engine: Engine):
    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def expected_baseline(workload: Workload, days: int) -> datetime:
    boundary = NOW - timedelta(days=days)
    if workload.name == "fallback":
        return max(boundary, NOW - timedelta(days=2))
    if workload.name == "composition":
        if days == 30:
            return NOW - timedelta(days=10)
        return boundary - timedelta(hours=1)
    return boundary


def inspect_output(engine: Engine, workload: Workload, environment, reader_type, service_type, *, batch=False) -> dict:
    with Session(engine) as session:
        reader = reader_type(session, environment=environment)
        summary = service_type(reader).view()
        latest_run = reader.latest_run()
        latest = reader.latest_snapshots(latest_run.id)
        assert summary.captured_at == NOW
        assert latest_run.captured_at == NOW
        if workload.name == "mixed":
            assert len(latest_run.errors) == (2 if environment is None else 1)
            assert len(latest) == (6 if environment is None else 3)
            assert [value.currency for value in summary.currencies] == ["RUB", "USD"]
        else:
            assert len(latest_run.errors) == 0
            assert len(latest) == (0 if environment == "PROD" else workload.accounts)
        traces = {}
        for currency in sorted({snapshot.currency for snapshot in latest}):
            current = tuple(snapshot for snapshot in latest if snapshot.currency == currency)
            traces[currency] = []
            boundaries = tuple(NOW - timedelta(days=days) for days in (1, 7, 30))
            values = (
                reader.common_baselines_many(current, boundaries)
                if batch
                else tuple(reader.common_baselines(current, boundary) for boundary in boundaries)
            )
            for days, baselines in zip((1, 7, 30), values, strict=True):
                assert len(baselines) == len(current)
                assert len({value.run_id for value in baselines}) == 1
                assert all(value.captured_at == expected_baseline(workload, days) for value in baselines)
                assert {(value.user_broker_id, value.account_id) for value in baselines} == {
                    (value.user_broker_id, value.account_id) for value in current
                }
                traces[currency].append(
                    {
                        "days": days,
                        "run_id": baselines[0].run_id,
                        "captured_at": baselines[0].captured_at,
                        "snapshot_ids": [value.id for value in baselines],
                    }
                )
            assert len({item["run_id"] for item in traces[currency]}) <= 3
            assert sum(len(item["snapshot_ids"]) for item in traces[currency]) <= 3 * len(current)
        if workload.name == "composition" and latest:
            assert broker_id(2) not in {value.user_broker_id for value in latest}
            assert broker_id(3) in {value.user_broker_id for value in latest}
            for boundary in (NOW - timedelta(days=1), NOW - timedelta(days=7)):
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(PortfolioSnapshotModel)
                        .where(
                            PortfolioSnapshotModel.user_broker_id == broker_id(3),
                            PortfolioSnapshotModel.captured_at == boundary,
                        )
                    )
                    == 0
                )
        return {"summary": summary.model_dump(mode="json"), "baselines": traces}


def nearest_rank_p95(values: list) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def measured_view(engine: Engine, environment, reader_type, service_type):
    with Session(engine) as session:
        service = service_type(reader_type(session, environment=environment))
        with capture_sql(engine) as statements:
            started = perf_counter_ns()
            summary = service.view()
            elapsed = (perf_counter_ns() - started) / 1_000_000
        assert all(statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement, _ in statements)
    return elapsed, summary.model_dump(mode="json"), statements


def explain(engine: Engine, statements: list) -> list:
    plans = []
    with engine.connect() as connection:
        for statement, parameters in statements:
            if "portfolio_snapshots" not in statement:
                continue
            if engine.dialect.name == "postgresql":
                plan = connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                ).scalar_one()
            else:
                plan = [tuple(row) for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)]
            plans.append({"sql": statement, "parameters": parameters, "plan": plan})
    return plans


def prepared_seed(engine, namespace, workload):
    seeded = seed(engine, workload)
    with engine.begin() as connection:
        if namespace is None:
            connection.exec_driver_sql("ANALYZE")
        else:
            for table in (
                UserBrokerModel.__tablename__,
                PortfolioSnapshotRunModel.__tablename__,
                PortfolioSnapshotModel.__tablename__,
            ):
                connection.exec_driver_sql(f'ANALYZE "{namespace}"."{table}"')
        if namespace is not None:
            seeded["schema_bytes"] = int(
                connection.scalar(
                    text(
                        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :namespace AND c.relkind = 'r'"
                    ),
                    {"namespace": namespace},
                )
            )
    return seeded


def profile_workload(engine, namespace, workload, reader_type, service_type):
    seeded = prepared_seed(engine, namespace, workload)
    scopes = {}
    for environment in SCOPES:
        label = environment or "ALL"
        expected = inspect_output(engine, workload, environment, reader_type, service_type)
        for _ in range(WARMUPS):
            _, summary, _ = measured_view(engine, environment, reader_type, service_type)
            assert summary == expected["summary"]
        samples = []
        sql_counts = []
        for _ in range(SAMPLES):
            elapsed, summary, statements = measured_view(engine, environment, reader_type, service_type)
            assert summary == expected["summary"]
            samples.append(elapsed)
            sql_counts.append(len(statements))
        assert len(set(sql_counts)) == 1
        scopes[label] = {
            "output": expected,
            "samples_ms": samples,
            "sql_counts": sql_counts,
            "median_ms": statistics.median(samples),
            "p95_ms": nearest_rank_p95(samples),
        }
    # Explain outside timed scopes, on the heavy grouped-run and snapshot paths.
    plans = []
    if workload.name in ("long", "composition"):
        _, _, statements = measured_view(engine, None, reader_type, service_type)
        plans = explain(engine, statements)
    return {"workload": workload.name, "seed": seeded, "scopes": scopes, "plans": plans}


def paired_workload(engine, namespace, workload, old_reader, old_service, baseline):
    seeded = prepared_seed(engine, namespace, workload)
    assert seeded["digest"] == baseline["seed"]["digest"]
    assert seeded["counts"] == baseline["seed"]["counts"]
    implementations = {
        "old": (old_reader, old_service),
        "new": (PortfolioSnapshotRepository, TradingSummaryService),
    }
    scopes = {}
    for environment in SCOPES:
        label = environment or "ALL"
        expected = inspect_output(engine, workload, environment, old_reader, old_service)
        actual = inspect_output(
            engine, workload, environment, PortfolioSnapshotRepository, TradingSummaryService, batch=True
        )
        expected = json.loads(json.dumps(expected, default=str))
        actual = json.loads(json.dumps(actual, default=str))
        assert expected == baseline["scopes"][label]["output"] == actual
        for _ in range(WARMUPS):
            for reader, service in implementations.values():
                _, output, _ = measured_view(engine, environment, reader, service)
                assert output == expected["summary"]
        samples = {"old": [], "new": []}
        counts = {"old": [], "new": []}
        pairs = []
        for index in range(SAMPLES):
            order = ("old", "new") if index % 2 == 0 else ("new", "old")
            pair = {"index": index, "order": order}
            for name in order:
                reader, service = implementations[name]
                elapsed, output, statements = measured_view(engine, environment, reader, service)
                assert output == expected["summary"]
                samples[name].append(elapsed)
                counts[name].append(len(statements))
                pair[name] = {"elapsed_ms": elapsed, "sql_count": len(statements)}
            pairs.append(pair)
        metrics = {
            name: {
                "samples_ms": samples[name],
                "sql_counts": counts[name],
                "median_ms": statistics.median(samples[name]),
                "p95_ms": nearest_rank_p95(samples[name]),
            }
            for name in implementations
        }
        for name in implementations:
            assert len(set(counts[name])) == 1
        assert counts["old"][0] == baseline["scopes"][label]["sql_counts"][0]
        budget = (
            (7 if environment is not None else 6) if workload.name == "mixed" else (2 if environment == "PROD" else 4)
        )
        median_limit = metrics["old"]["median_ms"] + max(1, 0.05 * metrics["old"]["median_ms"])
        p95_limit = metrics["old"]["p95_ms"] + max(2, 0.1 * metrics["old"]["p95_ms"])
        gates = {
            "sql_budget": counts["new"][0] == budget,
            "median": metrics["new"]["median_ms"] <= median_limit,
            "p95": metrics["new"]["p95_ms"] <= p95_limit,
        }
        scopes[label] = {
            "output": actual,
            "pairs": pairs,
            "metrics": metrics,
            "gates": gates,
            "limits": {"sql_budget": budget, "median_ms": median_limit, "p95_ms": p95_limit},
        }
        progress(
            f"C4 paired {workload.name}/{label}: gates {gates}; "
            f"actual SQL old/new {counts['old'][0]}/{counts['new'][0]}"
        )
    plans = {}
    if workload.name in ("long", "composition"):
        for name, (reader, service) in implementations.items():
            _, _, statements = measured_view(engine, None, reader, service)
            plans[name] = explain(engine, statements)
    return {"workload": workload.name, "seed": seeded, "scopes": scopes, "plans": plans}


def progress(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def main() -> None:
    if not __debug__:
        raise ValueError("C4 benchmark requires enabled self-checks; Python -O is forbidden.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "baseline", "paired"), required=True)
    parser.add_argument("--dialect", choices=("sqlite", "postgresql", "both"), default="both")
    parser.add_argument("--baseline-receipts", type=Path)
    parser.add_argument("--report-dir", type=Path, default=REPORTS)
    arguments = parser.parse_args()
    if arguments.phase == "paired" and arguments.baseline_receipts is None:
        parser.error("paired phase requires --baseline-receipts")
    # Guard precedes filesystem writes, schema creation, seed, migrations and measurements.
    url = validate_database_url(os.environ.get("POSTGRES_TEST_DATABASE_URL", ""))
    self_check()
    reports = validate_report_directory(arguments.report_dir)
    if arguments.phase == "prepare":
        freeze_sources(reports)
        progress("C4 prepared: exact reader/service frozen; self-checks PASS; no database connection.")
        return
    paired = arguments.phase == "paired"
    reader_type, service_type, sources = load_sources(paired=paired, reports=reports)
    baseline_directory = None
    if paired:
        baseline_directory = arguments.baseline_receipts.resolve()
        if baseline_directory.parent != reports.resolve():
            raise ValueError("Baseline receipts must be an existing C4 report directory.")
        baseline_summary = json.loads((baseline_directory / "summary.json").read_text())
        assert baseline_summary["phase"] == "baseline"
        assert baseline_summary["sources"] == sources
        assert digest_bytes((reports / "baseline_profile_script.py").read_bytes()) == sources["script_sha256"]
    run_directory = reports / f"{arguments.phase}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    run_directory.mkdir()
    dialects = ("sqlite", "postgresql") if arguments.dialect == "both" else (arguments.dialect,)
    receipts = []
    for dialect in dialects:
        for workload in WORKLOADS:
            progress(f"C4 {arguments.phase} START {dialect}/{workload.name}")
            with owned_engine(dialect, url, run_directory, workload) as (engine, namespace):
                if paired:
                    baseline = json.loads((baseline_directory / f"{dialect}-{workload.name}.json").read_text())
                    result = paired_workload(engine, namespace, workload, reader_type, service_type, baseline)
                else:
                    result = profile_workload(engine, namespace, workload, reader_type, service_type)
                result["dialect"] = dialect
                result["sources"] = sources
                if paired:
                    result["stage2_sources"] = {
                        name: digest_bytes((REPOSITORY / relative).read_bytes())
                        for name, relative in SOURCE_PATHS.items()
                    }
                    result["stage2_script_sha256"] = digest_bytes(Path(__file__).read_bytes())
                write_json(run_directory / f"{dialect}-{workload.name}.json", result)
                receipts.append(result)
            progress(
                f"C4 {arguments.phase} DONE {dialect}/{workload.name}: "
                + str(
                    {
                        key: value["gates"] if paired else value["sql_counts"][0]
                        for key, value in result["scopes"].items()
                    }
                ),
            )
    by_workload = {}
    for result in receipts:
        previous = by_workload.setdefault(result["workload"], result["seed"]["digest"])
        assert previous == result["seed"]["digest"]
    write_json(
        run_directory / "summary.json",
        {
            "phase": arguments.phase,
            "stage2_script_sha256": digest_bytes(Path(__file__).read_bytes()) if paired else None,
            "baseline_directory": str(baseline_directory) if paired else None,
            "sources": sources,
            "seed_phases": 2,
            "warmups": WARMUPS,
            "samples": SAMPLES,
            "max_history_rows": MAX_HISTORY_ROWS,
            "receipts": [f"{result['dialect']}-{result['workload']}.json" for result in receipts],
        },
    )
    progress(f"C4 {arguments.phase} receipts: {run_directory}")
    if paired and not all(all(scope["gates"].values()) for receipt in receipts for scope in receipt["scopes"].values()):
        raise SystemExit("C4 paired gates failed; first receipts retained. No favorable rerun.")


if __name__ == "__main__":
    main()
