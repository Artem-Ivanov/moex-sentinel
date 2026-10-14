"""Destructive helper contract, using synthetic data and an isolated database only."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.exc import OperationalError

from develop.scripts.purge_test_data import PurgeError, _predicate, main, run_purge
from moex_sentinel.storage.models import Base

pytest_plugins = ("tests.integration.postgresql.conftest",)
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request):
    if request.param == "postgresql":
        url = request.getfixturevalue("isolated_postgresql_database_url")
        engine = create_engine(url)
    else:
        engine = create_engine("sqlite://")
        event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def seed(engine):
    """Every classified scoped table has one TEST and one protected PROD record."""
    with engine.begin() as conn:
        runs = Base.metadata.tables["portfolio_snapshot_runs"]
        for n, name in enumerate(("pure", "shared", "neutral", "errors-only")):
            conn.execute(
                runs.insert().values(
                    id=name,
                    captured_at=NOW,
                    bucket_start=NOW + timedelta(minutes=n),
                    created_at=NOW,
                    safe_errors=(
                        []
                        if name in {"neutral", "pure"}
                        else [{"broker_id": "TEST", "message": "secret-test"}]
                        + ([{"broker_id": "PROD", "message": "protected"}] if name == "shared" else [])
                    ),
                )
            )
        for scope in ("TEST", "PROD"):
            records = {}
            for table in Base.metadata.sorted_tables:
                if table.name == "portfolio_snapshot_runs":
                    continue
                values = {}
                for col in table.columns:
                    if col.nullable:
                        values[col.name] = None
                    elif isinstance(col.type, Boolean):
                        values[col.name] = False
                    elif isinstance(col.type, JSON):
                        values[col.name] = {}
                    elif isinstance(col.type, DateTime) or col.type.__class__.__name__ == "UTCDateTime":
                        values[col.name] = NOW
                    elif isinstance(col.type, Date):
                        values[col.name] = date(2026, 10, 11)
                    elif isinstance(col.type, (Integer, Numeric)):
                        values[col.name] = 1
                    else:
                        values[col.name] = (
                            (scope + "-" + col.name)[: col.type.length] if isinstance(col.type, String) else scope
                        )
                for col in table.primary_key:
                    values[col.name] = (
                        scope
                        if table.name == "user_brokers" or col.name == "user_broker_id"
                        else (scope + "-" + table.name)[:36]
                    )
                if "user_broker_id" in values:
                    values["user_broker_id"] = scope
                for fk in table.foreign_key_constraints:
                    for el in fk.elements:
                        if el.column.table.name == "portfolio_snapshot_runs":
                            values[el.parent.name] = "pure" if scope == "TEST" else "shared"
                        else:
                            values[el.parent.name] = records[el.column.table.name][el.column.name]
                overrides = {
                    "user_brokers": {"environment": scope, "state": "ACTIVE", "external_account_id": "same-account"},
                    "worker_runtime_instances": {
                        "environment": scope,
                        "access_mode": "READ_ONLY",
                        "registration_status": "RETIRED",
                        "generation": 1,
                    },
                    "trading_account_admissions": {"admission_status": "DISARMED", "owner_generation": 1},
                    "trading_admission_baselines": {"currency": "RUB"},
                    "trading_admission_reconciliations": {"scope": "ACCOUNT", "status": "ACCOUNT_ONLY", "reasons": []},
                    "trading_submit_reservations": {
                        "state": "UNKNOWN",
                        "side": "BUY",
                        "idempotency_key": "repeated-key",
                    },
                    "trading_automations": {"state": "IN_WORK", "closed_at": None},
                    "position_cycles": {"state": "OPEN", "closed_at": None},
                    "trade_decisions": {"decision": "BUY_MORE"},
                    "broker_orders": {
                        "state": "UNCERTAIN",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "intent_kind": "OPEN",
                        "idempotency_key": "repeated-key",
                    },
                    "broker_order_events": {"to_state": "SUBMITTED"},
                    "trade_executions": {
                        "side": "BUY",
                        "source": "BROKER_FILL",
                        "external_execution_id": "repeated-execution",
                    },
                    "position_lots": {"source": "BROKER_EXECUTION"},
                    "trade_audit_events": {"level": "CRITICAL", "critical": True},
                    "instrument_sync_state": {"status": "SUCCESS"},
                }.get(table.name, {})
                values.update(overrides)
                conn.execute(table.insert().values(**values))
                records[table.name] = values
            snapshot = dict(
                records["portfolio_snapshots"],
                id=scope + "-shared",
                run_id="shared",
                bucket_start=NOW + timedelta(minutes=1),
            )
            if scope == "TEST":
                conn.execute(Base.metadata.tables["portfolio_snapshots"].insert().values(**snapshot))
                conn.execute(
                    Base.metadata.tables["portfolio_snapshots"]
                    .insert()
                    .values(**dict(records["portfolio_snapshots"], id="TEST-pure-second", currency="USD"))
                )


def contents(engine):
    with engine.connect() as conn:
        return {
            t.name: [dict(r) for r in conn.execute(select(t).order_by(*t.primary_key)).mappings()]
            for t in Base.metadata.sorted_tables
        }


def test_mixed_graph_dry_run_apply_and_repeat(database):
    seed(database)
    before = contents(database)
    report = run_purge(database)
    assert report["candidates"]["user_brokers"] == 1
    assert contents(database) == before
    result = run_purge(database, apply=True, writers_fenced=True, batch_size=1)
    assert result["deleted"]["user_brokers"] == 1
    after = contents(database)
    for table, rows in before.items():
        if table != "portfolio_snapshot_runs":
            assert after[table] == [r for r in rows if r.get("user_broker_id", r.get("id")) == "PROD"]
    assert {r["id"] for r in after["portfolio_snapshot_runs"]} == {"shared", "neutral"}
    shared = next(r for r in after["portfolio_snapshot_runs"] if r["id"] == "shared")
    expected = next(r for r in before["portfolio_snapshot_runs"] if r["id"] == "shared")
    assert shared == dict(expected, safe_errors=[{"broker_id": "PROD", "message": "protected"}])
    assert not any(run_purge(database, apply=True, writers_fenced=True)["deleted"].values())


def test_requires_fence_and_rejects_unknown_schema(database):
    seed(database)
    before = contents(database)
    with pytest.raises(PurgeError, match="WRITERS_NOT_FENCED"):
        run_purge(database, apply=True)
    with database.begin() as conn:
        conn.execute(text("CREATE TABLE unexpected (id INTEGER PRIMARY KEY)"))
    with pytest.raises(PurgeError, match="UNSUPPORTED_SCHEMA"):
        run_purge(database, apply=True, writers_fenced=True)
    assert contents(database) == before


def test_protected_cross_scope_reference_refuses_before_mutation(database):
    seed(database)
    with database.begin() as conn:
        conn.execute(
            text(
                "UPDATE trading_account_admissions SET recovery_runtime_instance_id = 'TEST-worker_runtime_instances' "
                "WHERE user_broker_id = 'PROD'"
            )
        )
    before = contents(database)
    with pytest.raises(PurgeError, match="PROTECTED_REFERENCE"):
        run_purge(database, apply=True, writers_fenced=True)
    assert contents(database) == before


def test_midbatch_failure_and_restart(database):
    seed(database)
    calls = []

    def fail(progress):
        calls.append(progress)
        if sum(progress["deleted"].values()) >= 2:
            raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated"):
        run_purge(database, apply=True, writers_fenced=True, batch_size=1, progress=fail)
    assert calls
    run_purge(database, apply=True, writers_fenced=True, batch_size=1)
    assert not any(run_purge(database)["candidates"].values())


def test_scope_drift_stops_before_deleting_newly_protected_rows(database):
    seed(database)
    changed = False
    protected_after_drift = None

    def drift(_):
        nonlocal changed, protected_after_drift
        if not changed:
            with database.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE user_brokers SET environment = 'PROD', external_account_id = 'changed-account' "
                        "WHERE id = 'TEST'"
                    )
                )
            protected_after_drift = contents(database)
            changed = True

    with pytest.raises(PurgeError, match="SCOPE_DRIFT"):
        run_purge(database, apply=True, writers_fenced=True, batch_size=1, progress=drift)
    assert contents(database) == protected_after_drift


def test_concurrent_prod_write_is_preserved(database):
    seed(database)
    changed = False

    def insert_prod(_):
        nonlocal changed
        if not changed:
            with database.begin() as conn:
                table = Base.metadata.tables["automation_events"]
                row = dict(conn.execute(select(table).where(table.c.user_broker_id == "PROD")).mappings().one())
                conn.execute(table.insert().values(**dict(row, event_id="concurrent-prod", sequence_number=2)))
            changed = True

    run_purge(database, apply=True, writers_fenced=True, batch_size=1, progress=insert_prod)
    assert any(r["event_id"] == "concurrent-prod" for r in contents(database)["automation_events"])


def test_exact_environment_predicate_does_not_treat_production_or_null_as_test():
    metadata = MetaData()
    scopes = Table("user_brokers", metadata, Column("id", String, primary_key=True), Column("environment", String))
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            scopes.insert(),
            [{"id": str(n), "environment": e} for n, e in enumerate(("TEST", "PROD", "PRODUCTION", None))],
        )
        assert list(conn.scalars(select(scopes.c.id).where(_predicate(scopes, metadata.tables)))) == ["0"]
    engine.dispose()


def test_cli_never_echoes_invalid_url(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "SECRET-NOT-A-URL")
    assert main([]) == 2
    output = capsys.readouterr()
    assert "SECRET" not in output.err + output.out
    assert '"error": "PURGE_FAILED"' in output.err


def test_database_failure_rolls_back_current_batch_and_keyset_resumes(database):
    seed(database)
    with database.begin() as conn:
        table = Base.metadata.tables["automation_events"]
        row = dict(conn.execute(select(table).where(table.c.user_broker_id == "TEST")).mappings().one())
        for n in range(2, 6):
            conn.execute(table.insert().values(**dict(row, event_id=f"TEST-event-{n}", sequence_number=n)))
    deletes = 0
    queries = []

    def fail_on_second_delete(conn, cursor, statement, parameters, context, executemany):
        nonlocal deletes
        if statement.startswith("SELECT automation_events.event_id"):
            queries.append(statement)
        if statement.startswith("DELETE FROM automation_events"):
            deletes += 1
            if deletes == 2:
                raise OperationalError(statement, parameters, Exception("secret-driver-message"))

    event.listen(database, "before_cursor_execute", fail_on_second_delete)
    try:
        with pytest.raises(PurgeError, match="^DATABASE_OPERATION_FAILED$"):
            run_purge(database, apply=True, writers_fenced=True, batch_size=1)
    finally:
        event.remove(database, "before_cursor_execute", fail_on_second_delete)
    assert deletes == 2
    assert "automation_events.event_id >" in queries[-1]
    assert len([r for r in contents(database)["automation_events"] if r["user_broker_id"] == "TEST"]) == 4
    run_purge(database, apply=True, writers_fenced=True, batch_size=1)
    assert len(contents(database)["automation_events"]) == 1
