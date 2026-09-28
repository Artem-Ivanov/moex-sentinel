"""Compare the current selector with the frozen pre-W2b SQLite oracle."""

from __future__ import annotations

import argparse
import gzip
import json
import platform
import sqlite3
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copyfileobj
from uuid import UUID

import sqlalchemy
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from develop.benchmarks.fact_outbox_baseline_oracle import select_old_batch
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base, CachedAutomationModel, FactOutboxModel
from trading_automaton.storage.repository import LocalAutomationRepository

SEED = 20260926
SCOPE_ID = "00000000-0000-4000-8000-000000000001"
BASE_TIME = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
BOOTSTRAP_CYCLE = "00000000-0000-4000-8000-000000000701"
BOOTSTRAP_LOT = "00000000-0000-4000-8000-000000000702"
BOOTSTRAP_KINDS = (
    "POSITION_CYCLE_UPDATED",
    "POSITION_LOT_OPENED",
    "TRADE_AUDIT_RECORDED",
    "AUTOMATION_STATE_CHANGED",
)


def _event_id(index: int) -> str:
    return str(UUID(int=index + 1))


def dataset(path: Path, count: int, *, scenario: str) -> sessionmaker[Session]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    engine = create_worker_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    automation_count = min(500, max(5, count // 20))
    bootstrap_contexts = {0: (BOOTSTRAP_CYCLE, BOOTSTRAP_LOT)} if scenario == "bootstrap" else {}
    if scenario == "many-bootstrap":
        bootstrap_contexts = {
            index: (str(UUID(int=700_000 + index)), str(UUID(int=800_000 + index)))
            for index in range(automation_count)
            if index not in (1, 2)
        }
    with factory.begin() as session:
        session.add_all(
            CachedAutomationModel(
                automation_id=str(UUID(int=100_000 + index)),
                user_broker_id=SCOPE_ID,
                broker_id=SCOPE_ID,
                account_id="synthetic",
                instrument_id="synthetic",
                fact_instrument_id=SCOPE_ID,
                currency="RUB",
                position_cycle_id=None,
                lot_size=1,
                min_price_increment="0.01",
                state="IN_WORK",
                revision=1,
                last_sequence_number=0,
                resume_requested=False,
                bootstrap_position_cycle_id=bootstrap_contexts[index][0] if index in bootstrap_contexts else None,
                bootstrap_position_lot_id=bootstrap_contexts[index][1] if index in bootstrap_contexts else None,
            )
            for index in range(automation_count)
        )
        session.flush()
        rows: list[FactOutboxModel] = []
        used = 0
        sequences: dict[int, int] = {}

        def add_row(
            automation_index: int,
            *,
            event_number: int,
            kind: str = "TRADE_AUDIT_RECORDED",
            payload: dict[str, object] | None = None,
            state: str = "PENDING",
            retry_at: datetime | None = None,
            occurred_ms: int | None = None,
        ) -> None:
            sequence = sequences.get(automation_index, 0) + 1
            sequences[automation_index] = sequence
            rows.append(
                FactOutboxModel(
                    event_id=_event_id(event_number),
                    user_broker_id=SCOPE_ID,
                    automation_id=str(UUID(int=100_000 + automation_index)),
                    sequence_number=sequence,
                    expected_revision=1,
                    fact_kind=kind,
                    payload=payload or {"stage": "SYNTHETIC", "sample": (event_number * 104_729 + SEED) % 1_000_000},
                    safe_message="Synthetic selector-only row",
                    occurred_at=BASE_TIME
                    + timedelta(milliseconds=occurred_ms if occurred_ms is not None else sequence),
                    created_at=BASE_TIME,
                    updated_at=BASE_TIME,
                    delivery_state=state,
                    retry_count=1 if retry_at is not None else 0,
                    next_retry_at=retry_at,
                )
            )

        for index, (cycle_id, lot_id) in bootstrap_contexts.items():
            payloads = (
                {"position_cycle_id": cycle_id},
                {"position_cycle_id": cycle_id, "position_lot_id": lot_id, "source": "BROKER_POSITION_BOOTSTRAP"},
                {"stage": "BOOTSTRAP_POSITION_ADOPTED"},
                {"state": "IN_WORK"},
            )
            for kind, payload in zip(BOOTSTRAP_KINDS, payloads, strict=True):
                add_row(index, event_number=used, kind=kind, payload=payload)
                used += 1
        add_row(1, event_number=used, retry_at=BASE_TIME + timedelta(days=4), occurred_ms=1)
        used += 1
        add_row(1, event_number=used, occurred_ms=2)
        used += 1
        add_row(2, event_number=used, state="FAILED", occurred_ms=3)
        used += 1
        normal_start = used if scenario == "many-bootstrap" else (4 if scenario == "bootstrap" else 3)
        while used < count:
            automation_index = 3 + ((used - normal_start) % (automation_count - 3))
            add_row(automation_index, event_number=used)
            used += 1
        session.add_all(rows)
    return factory


def measure(factory: sessionmaker[Session], selector, *, repeats: int) -> dict[str, object]:
    engine = factory.kw["bind"]
    query_count = 0

    def count_query(_connection, _cursor, _statement, _parameters, _context, _many):
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    times_ms = []
    peak_bytes = 0
    selected_ids: list[str] = []
    try:
        selector(factory, 100, now=BASE_TIME + timedelta(days=2), deadline_ms=0)
        query_count = 0
        for _ in range(repeats):
            tracemalloc.start()
            started = time.perf_counter_ns()
            selected_ids = selector(factory, 100, now=BASE_TIME + timedelta(days=2), deadline_ms=0)
            times_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_bytes = max(peak_bytes, peak)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    return {
        "repeats": repeats,
        "sql_statements_total": query_count,
        "sql_statements_per_selection": query_count / repeats,
        "selection_ms_median": statistics.median(times_ms),
        "selection_ms_min": min(times_ms),
        "selection_ms_max": max(times_ms),
        "tracemalloc_peak_bytes_max": peak_bytes,
        "selected_event_ids_in_order": selected_ids,
    }


def current_selector(factory, limit: int, *, now: datetime, deadline_ms: int) -> list[str]:
    return [
        row.event_id
        for row in LocalAutomationRepository(factory).ready_fact_outbox(limit, now=now, deadline_ms=deadline_ms)
    ]


def create_baseline(args) -> None:
    result_path = args.output / "selector-baseline-v2.json"
    if result_path.exists() and not args.force:
        raise SystemExit(f"Refusing to replace {result_path}; pass --force to write a new baseline")
    result = {
        "oracle": (
            "select_old_batch frozen pre-W2b implementation in "
            "tests/trading_automaton/storage/fact_outbox_baseline_oracle.py"
        ),
        "seed": SEED,
        "limits": {"selection": 100, "deadline_ms": 0},
        "synthetic_row_note": (
            "Selector-only audit records use expected_revision=1; sequence allocation is realistic per automation, "
            "payloads do not claim full domain validation."
        ),
        "scenarios": {},
    }
    for scenario in ("normal", "bootstrap"):
        result["scenarios"][scenario] = {}
        for count in args.sizes:
            path = args.output / "datasets" / f"fact-outbox-{scenario}-{count}.sqlite"
            compressed_path = path.with_suffix(path.suffix + ".gz")
            factory = dataset(path, count, scenario=scenario)
            try:
                oracle = measure(factory, select_old_batch, repeats=args.repeats)
                current = measure(factory, current_selector, repeats=args.repeats)
            finally:
                factory.kw["bind"].dispose()
            uncompressed_bytes = path.stat().st_size
            with path.open("rb") as source, gzip.open(compressed_path, "wb", compresslevel=9) as target:
                copyfileobj(source, target)
            path.unlink()
            result["scenarios"][scenario][str(count)] = {
                "dataset_path": str(compressed_path),
                "dataset_bytes_compressed": compressed_path.stat().st_size,
                "dataset_bytes_uncompressed": uncompressed_bytes,
                "row_count": count,
                "valid_bootstrap_quartet": scenario == "bootstrap",
                "oracle_old_selector": oracle,
                "current_selector": current,
                "current_ids_match_old_oracle": current["selected_event_ids_in_order"]
                == oracle["selected_event_ids_in_order"],
            }
    result["environment"] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlalchemy": sqlalchemy.__version__,
        "sqlite": sqlite3.sqlite_version,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"{result_path}\n")


def compare_baseline(args, *, selector=None) -> None:
    result_path = args.output / "selector-baseline-v2.json"
    baseline = json.loads(result_path.read_text(encoding="utf-8"))
    selector = selector or current_selector
    comparison = {"baseline_path": str(result_path), "scenarios": {}}
    mismatches = []
    for scenario, sizes in baseline["scenarios"].items():
        comparison["scenarios"][scenario] = {}
        for size, case in sizes.items():
            if int(size) not in args.sizes:
                continue
            compressed = Path(case["dataset_path"])
            if not compressed.is_absolute() and not compressed.exists():
                compressed = args.output / "datasets" / compressed.name
            path = args.output / "datasets" / f"compare-{scenario}-{size}.sqlite"
            with gzip.open(compressed, "rb") as source, path.open("wb") as target:
                copyfileobj(source, target)
            engine = create_worker_engine(f"sqlite:///{path}")
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            try:
                oracle = measure(factory, select_old_batch, repeats=args.repeats)
                current = measure(factory, selector, repeats=args.repeats)
            finally:
                engine.dispose()
                path.unlink(missing_ok=True)
            oracle_matches = (
                oracle["selected_event_ids_in_order"] == case["oracle_old_selector"]["selected_event_ids_in_order"]
            )
            current_matches = (
                current["selected_event_ids_in_order"] == case["oracle_old_selector"]["selected_event_ids_in_order"]
            )
            if not oracle_matches:
                mismatches.append(f"frozen oracle changed: {scenario}/{size}")
            if not current_matches:
                mismatches.append(f"current selector differs from old oracle: {scenario}/{size}")
            comparison["scenarios"][scenario][size] = {
                "oracle_expected_ids_unchanged": oracle_matches,
                "current_ids_match_old_oracle": current_matches,
                "oracle_old_selector": oracle,
                "current_selector": current,
            }
    compare_path = args.output / "selector-comparison-v2.json"
    compare_path.write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"{compare_path}\n")
    if mismatches:
        raise SystemExit("; ".join(mismatches))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("develop/reports/0.9.8-w2"))
    parser.add_argument("--mode", choices=("compare", "write-baseline"), default="compare")
    parser.add_argument("--force", action="store_true", help="allow replacing selector-baseline-v2.json")
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 10_000, 50_000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "write-baseline":
        create_baseline(args)
    else:
        compare_baseline(args)


if __name__ == "__main__":
    main()
