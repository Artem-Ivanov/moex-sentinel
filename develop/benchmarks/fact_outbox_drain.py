"""Measure complete eligible selector drain on a frozen persisted SQLite queue."""

import argparse
import gzip
import hashlib
import json
import platform
import resource
import statistics
import sys
import time
import tracemalloc
from datetime import timedelta
from math import ceil
from pathlib import Path
from shutil import copyfileobj
from tempfile import TemporaryDirectory

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.orm import sessionmaker

from develop.benchmarks.benchmark_fact_outbox_selector import BASE_TIME, SEED, current_selector, dataset
from develop.benchmarks.fact_outbox_baseline_oracle import select_old_batch
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import FactOutboxModel


def _distribution(samples):
    ordered = sorted(samples)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None, "p99": None}
    return {
        "samples": len(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[ceil(len(ordered) * 0.95) - 1],
        "p99": ordered[ceil(len(ordered) * 0.99) - 1],
    }


def measure_drain(factory, *, selector, batch_size):
    """Select and bulk-remove eligible IDs; no HTTP or Core work is included."""
    if batch_size < 1 or selector not in {"old", "current"}:
        raise ValueError("selector must be old/current and batch_size positive")
    choose = select_old_batch if selector == "old" else current_selector
    engine = factory.kw["bind"]
    all_sql = selection_sql = 0
    selecting = False

    def count_sql(_connection, _cursor, _statement, _parameters, _context, _many):
        nonlocal all_sql, selection_sql
        all_sql += 1
        if selecting:
            selection_sql += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    selection_ms = []
    batches = 0
    digest = hashlib.sha256()
    delivered = 0
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    tracemalloc.start()
    try:
        while True:
            selecting = True
            started = time.perf_counter()
            try:
                ids = choose(factory, batch_size, now=BASE_TIME + timedelta(days=2), deadline_ms=0)
            finally:
                selecting = False
            selection_ms.append((time.perf_counter() - started) * 1000)
            if not ids:
                break
            batches += 1
            if len(ids) != len(set(ids)):
                raise AssertionError("Selector returned duplicate event IDs")
            with factory.begin() as session:
                deleted = session.execute(delete(FactOutboxModel).where(FactOutboxModel.event_id.in_(ids)))
                if deleted.rowcount != len(ids):
                    raise AssertionError("Selector did not make progress")
            for event_id in ids:
                digest.update(event_id.encode("ascii"))
                digest.update(b"\n")
            delivered += len(ids)
        with factory() as session:
            remaining = session.scalar(select(func.count()).select_from(FactOutboxModel))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        event.remove(engine, "before_cursor_execute", count_sql)
    return {
        "selector": selector,
        "batch_size": batch_size,
        "delivered_facts": delivered,
        "remaining_facts": remaining,
        "batches": batches,
        "delivery_order_sha256": digest.hexdigest(),
        "selection_ms": _distribution(selection_ms),
        "selection_total_ms": sum(selection_ms),
        "drain_wall_ms": (time.perf_counter() - started_wall) * 1000,
        "drain_cpu_ms": (time.process_time() - started_cpu) * 1000,
        "tracemalloc_peak_bytes": peak_bytes,
        "process_maxrss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "process_maxrss_unit": "bytes" if platform.system() == "Darwin" else "KiB",
        "selection_sql_statements": selection_sql,
        "all_sql_statements": all_sql,
        "scope": "Worker SQLite selector plus bulk deletion of selected IDs; excludes HTTP/Core",
        "database_rows_scanned": "not measured by SQLite cursor instrumentation",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--dataset", type=Path, help="Frozen .sqlite.gz baseline")
    input_group.add_argument(
        "--many-bootstrap-count", type=int, help="Generate many valid quartets with the fixed seed"
    )
    parser.add_argument("--selector", choices=("old", "current"), required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--block-all", action="store_true", help="make every PENDING head retry in the future")
    parser.add_argument("--implementation-label", default="unspecified")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="sentinel-outbox-drain-") as folder:
        path = Path(folder) / "queue.sqlite"
        if args.dataset is not None:
            with gzip.open(args.dataset, "rb") as source, path.open("wb") as target:
                copyfileobj(source, target)
            engine = create_worker_engine(f"sqlite:///{path}")
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            dataset_label = str(args.dataset)
        else:
            if args.many_bootstrap_count < 15:
                parser.error("many-bootstrap-count must be at least 15")
            factory = dataset(path, args.many_bootstrap_count, scenario="many-bootstrap")
            engine = factory.kw["bind"]
            dataset_label = f"many-bootstrap-{args.many_bootstrap_count}-seed-{SEED}"
        try:
            if args.block_all:
                with factory.begin() as session:
                    session.execute(
                        update(FactOutboxModel)
                        .where(FactOutboxModel.delivery_state == "PENDING")
                        .values(next_retry_at=BASE_TIME + timedelta(days=4))
                    )
            result = measure_drain(factory, selector=args.selector, batch_size=args.batch_size)
        finally:
            engine.dispose()
    sys.stdout.write(
        json.dumps(
            {
                "dataset": dataset_label,
                "block_all": args.block_all,
                "implementation": args.implementation_label,
                **result,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
