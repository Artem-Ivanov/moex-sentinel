"""Bounded lookup diagnosis on synthetic history in a freshly created test schema."""

# ruff: noqa: INP001, T201

import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import and_, event, insert, or_, schema, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from develop.benchmarks.runtime import distribution, require
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import AutomationEventModel, Base
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository
from tests.storage.test_trading_facts_models import automation_model
from tests.storage.trading_facts_helpers import NOW, instrument_model, user_broker_model

ROOT = Path(__file__).resolve().parent
SCOPE = "scope-1"
AUTOMATIONS = 50
FACTS_PER_AUTOMATION = 1000
PAIRS = 150


@contextmanager
def isolated_engine():
    url = make_url(os.environ["POSTGRES_TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise ValueError("POSTGRES_TEST_DATABASE_URL must identify the separate PostgreSQL test database")
    bounded_url = url.update_query_dict({"options": "-cstatement_timeout=20000 -clock_timeout=5000"})
    admin = create_database_engine(bounded_url)
    schema_name = f"lookup_probe_{uuid4().hex}"
    created = False
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(schema.CreateSchema(schema_name))
        created = True
        scoped_url = bounded_url.update_query_dict(
            {"options": f"-csearch_path={schema_name} -cstatement_timeout=20000 -clock_timeout=5000"}
        )
        engine = create_database_engine(scoped_url)
        Base.metadata.create_all(engine)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(schema.DropSchema(schema_name, cascade=True))
        finally:
            admin.dispose()


def seed_history(engine):
    with Session(engine) as session, session.begin():
        session.add(user_broker_model(SCOPE, "synthetic-account"))
        session.flush()
        session.add(instrument_model("instrument-1", SCOPE))
        session.flush()
        for number in range(AUTOMATIONS):
            automation = automation_model(f"automation-{number}", state="CLOSED", closed=True)
            automation.last_sequence_number = FACTS_PER_AUTOMATION
            session.add(automation)
    with engine.begin() as connection:
        for number in range(AUTOMATIONS):
            connection.execute(
                insert(AutomationEventModel),
                [
                    {
                        "event_id": str(UUID(int=number * FACTS_PER_AUTOMATION + sequence)),
                        "user_broker_id": SCOPE,
                        "automation_id": f"automation-{number}",
                        "sequence_number": sequence,
                        "expected_revision": 1,
                        "fact_kind": "TRADE_AUDIT_RECORDED",
                        "safe_message": "Synthetic lookup history",
                        "payload": {"source": "synthetic-query-plan-probe", "value": sequence},
                        "occurred_at": NOW,
                        "received_at": NOW,
                    }
                    for sequence in range(1, FACTS_PER_AUTOMATION + 1)
                ],
            )


def explain_lookups(engine):
    event_id = str(UUID(int=1_000_000))
    scope = AutomationEventModel.user_broker_id == SCOPE
    identity = AutomationEventModel.event_id == event_id
    sequence = and_(
        AutomationEventModel.automation_id == "automation-0",
        AutomationEventModel.sequence_number == FACTS_PER_AUTOMATION + 1,
    )
    queries = {
        "after_combined": select(AutomationEventModel).where(scope, or_(identity, sequence)),
        "before_event": select(AutomationEventModel).where(scope, identity),
        "before_sequence": select(AutomationEventModel).where(scope, sequence),
    }
    plans = {}
    with engine.connect() as connection:
        for name, statement in queries.items():
            compiled = statement.compile(dialect=connection.dialect)
            plan = connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}", compiled.params
            ).scalar_one()
            plans[name] = {"sql": str(compiled), "plan": plan}
    return plans


def baseline_repository(session):
    path = ROOT / "before/src/moex_sentinel/storage/repositories/trading_audit.py"
    spec = importlib.util.spec_from_file_location("lookup_probe_baseline_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TradingAuditRepository(session)


def measure_pairs(engine):
    durations = {"before": [], "after": []}
    query_counts = {"before": 0, "after": 0}
    active_version = None

    def count_query(_connection, _cursor, _statement, _parameters, _context, _many):
        if active_version is not None:
            query_counts[active_version] += 1

    with Session(engine) as session:
        before = baseline_repository(session)
        after = TradingAuditRepository(session)

        def lookup(version, iteration):
            event_id = str(UUID(int=1_000_000 + iteration))
            automation_id = f"automation-{iteration % AUTOMATIONS}"
            sequence = FACTS_PER_AUTOMATION + 1 + iteration
            if version == "before":
                by_event = before.get_envelope_by_event_id(SCOPE, event_id)
                by_sequence = before.get_envelope_by_sequence(SCOPE, automation_id, sequence)
                require(by_event is by_sequence is None, "Baseline unexpectedly matched synthetic new keys")
            else:
                require(
                    after.find_envelope_matches(SCOPE, event_id, automation_id, sequence) == (),
                    "Combined lookup unexpectedly matched synthetic new keys",
                )

        # Warm both query shapes and the connection before recording any timings.
        for iteration in range(10):
            lookup("before", iteration)
            lookup("after", iteration)
        event.listen(engine, "before_cursor_execute", count_query)
        try:
            for iteration in range(PAIRS):
                order = ("before", "after") if iteration % 2 == 0 else ("after", "before")
                for version in order:
                    active_version = version
                    started = perf_counter()
                    lookup(version, iteration)
                    durations[version].append((perf_counter() - started) * 1000)
                    active_version = None
        finally:
            event.remove(engine, "before_cursor_execute", count_query)
    require(query_counts == {"before": PAIRS * 2, "after": PAIRS}, "Lookup query budget changed")
    return {
        "pairs": PAIRS,
        "query_counts": query_counts,
        "repository_lookup_ms": {version: distribution(values) for version, values in durations.items()},
        "before_minus_after_ms": distribution(
            [old - new for old, new in zip(durations["before"], durations["after"], strict=True)]
        ),
    }


def main():
    with isolated_engine() as engine:
        seed_history(engine)
        before_analyze = explain_lookups(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("ANALYZE automation_events")
        after_analyze = explain_lookups(engine)
        measurements = measure_pairs(engine)
        report = {
            "schema_version": 1,
            "postgresql_version": ".".join(str(part) for part in engine.dialect.server_version_info),
            "automations": AUTOMATIONS,
            "envelopes_per_automation": FACTS_PER_AUTOMATION,
            "envelopes": AUTOMATIONS * FACTS_PER_AUTOMATION,
            "conditions": "50 CLOSED automations in one scope, missing event and new sequence; synthetic payloads",
            "limitations": (
                "Base metadata schema, not migrated ingress; "
                "warm single-session repository calls after manual ANALYZE; "
                "not the end-to-end acceptance workload. Before manual ANALYZE may include automatic statistics."
            ),
            "before_manual_analyze": before_analyze,
            "after_manual_analyze": after_analyze,
            "measurements": measurements,
        }
    # The temporary schema is gone before publishing the diagnostic artifact.
    destination = ROOT.parents[1] / "reports/postgresql-profile-envelope-query-plan-2026-09-10.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
