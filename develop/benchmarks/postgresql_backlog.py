"""Drain a valid supporting-fact backlog through Worker SQLite, HTTP, and Core PostgreSQL."""

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import tracemalloc
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from sqlalchemy import func, insert, select

from develop.benchmarks.postgresql import PostgreSQLHarness, isolated_database
from develop.benchmarks.runtime import distribution
from develop.benchmarks.synthetic import START
from moex_sentinel.storage.models import AutomationEventModel, TradeAuditEventModel, TradingAutomationModel
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel


def _seed_backlog(harness, count):
    value = harness.values[0]
    automation_id = str(value.automation_id)
    scope_id = str(value.user_broker_id)
    instrument_id = str(value.instrument_id)
    with harness.factory.begin() as session:
        for offset in range(0, count, 1000):
            rows = []
            for index in range(offset, min(offset + 1000, count)):
                occurred_at = START + timedelta(milliseconds=index)
                rows.append(
                    {
                        "event_id": str(UUID(int=1_000_000 + index)),
                        "user_broker_id": scope_id,
                        "automation_id": automation_id,
                        "sequence_number": index + 1,
                        "expected_revision": 1,
                        "fact_kind": "TRADE_AUDIT_RECORDED",
                        "payload": {
                            "audit_event_id": str(UUID(int=2_000_000 + index)),
                            "process_id": str(UUID(int=3_000_000 + index)),
                            "parent_process_id": None,
                            "decision_id": None,
                            "broker_order_id": None,
                            "execution_id": None,
                            "instrument_id": instrument_id,
                            "level": "INFO",
                            "stage": "SYNTHETIC_BACKLOG",
                            "safe_message": "Synthetic backlog audit",
                            "data": {"index": index},
                            "occurred_at": occurred_at.isoformat(),
                            "created_at": occurred_at.isoformat(),
                            "critical": False,
                        },
                        "safe_message": "Synthetic backlog audit",
                        "occurred_at": occurred_at,
                        "created_at": START,
                        "updated_at": START,
                        "delivery_state": "PENDING",
                        "retry_count": 0,
                    }
                )
            session.execute(insert(FactOutboxModel), rows)
        session.get_one(CachedAutomationModel, automation_id).last_sequence_number = count


async def run_case(database_url, count, *, batch_size=1000):
    if count < 1 or batch_size < 1:
        raise ValueError("count and batch_size must be positive")
    with isolated_database(database_url) as engine, TemporaryDirectory(prefix="sentinel-pg-backlog-") as folder:
        harness = PostgreSQLHarness(Path(folder), 1, engine, batch_size)
        try:
            harness.initialize()
            _seed_backlog(harness, count)
            initial_backlog = harness.count(FactOutboxModel)
            requests = 0
            flush_ms = []
            with httpx.Client(base_url=harness.server.url, timeout=120, trust_env=False) as http:
                synchronizer = FactSynchronizationService(
                    harness.repository,
                    CoreClient(http),
                    now=lambda: START + timedelta(days=2),
                    sleep=lambda _: None,
                    retry_limit=0,
                    batch_size=batch_size,
                    deadline_ms=0,
                )
                started_wall = time.perf_counter()
                started_cpu = time.process_time()
                tracemalloc.start()
                try:
                    while harness.count(FactOutboxModel):
                        before = harness.count(FactOutboxModel)
                        started = time.perf_counter()
                        if not synchronizer.flush_outbox():
                            raise AssertionError("Synthetic HTTP backlog delivery failed")
                        flush_ms.append((time.perf_counter() - started) * 1000)
                        requests += 1
                        if harness.count(FactOutboxModel) >= before:
                            raise AssertionError("Synthetic HTTP backlog made no progress")
                    _, peak_bytes = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            wall_ms = (time.perf_counter() - started_wall) * 1000
            cpu_ms = (time.process_time() - started_cpu) * 1000
            with harness.core_factory() as session:
                core_events = session.scalar(select(func.count()).select_from(AutomationEventModel))
                core_audits = session.scalar(select(func.count()).select_from(TradeAuditEventModel))
                core = session.get_one(TradingAutomationModel, str(harness.values[0].automation_id))
                core_sequence, core_revision = core.last_sequence_number, core.revision
            with harness.factory() as session:
                worker = session.get_one(CachedAutomationModel, str(harness.values[0].automation_id))
                worker_sequence, worker_revision = worker.last_sequence_number, worker.revision
            if (core_events, core_audits, core_sequence, worker_sequence) != (count, count, count, count):
                raise AssertionError("Worker/Core facts or final sequences differ")
            core_profile = harness.profile.report()
            # This workload seeds persisted rows directly, so Profile.first_attempt()
            # never runs and its delivery counter does not describe this drain.
            del core_profile["delivered_facts"]
            return {
                "count": count,
                "batch_size": batch_size,
                "initial_backlog": initial_backlog,
                "worker_remaining": harness.count(FactOutboxModel),
                "core_events": core_events,
                "core_audits": core_audits,
                "core_sequence": core_sequence,
                "worker_sequence": worker_sequence,
                "core_revision": core_revision,
                "worker_revision": worker_revision,
                "requests": requests,
                "wall_ms": wall_ms,
                "cpu_ms": cpu_ms,
                "tracemalloc_peak_bytes": peak_bytes,
                "flush_ms": distribution(flush_ms),
                "core_profile": core_profile,
                "postgresql_version": ".".join(str(part) for part in engine.dialect.server_version_info),
                "python": platform.python_version(),
                "workload": "one automation, sequential audit facts without linked trade objects",
                "scope": "Worker SQLite selector and ACK, real HTTP, Core PostgreSQL ingress and commit",
            }
        finally:
            await harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        parser.error("POSTGRES_TEST_DATABASE_URL must point to a disposable PostgreSQL test database")
    sys.stdout.write(
        json.dumps(asyncio.run(run_case(database_url, args.count, batch_size=args.batch_size)), indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
