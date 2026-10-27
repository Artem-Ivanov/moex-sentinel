"""Profile Worker D1 diagnostics against synthetic, isolated SQLite databases.

Run from the repository root with ``PYTHONPATH=src .venv/bin/python ...``.
No existing Worker database is opened or copied.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import create_engine

from trading_automaton.storage.execution_diagnostics import (
    _ACTIVE_STATES,
    _AUTOMATIONS,
    _FACT_COUNTS,
    _INTENTS,
    _TERMINAL,
    _UNSCOPED_CACHE,
    WorkerExecutionDiagnosticsReader,
)
from trading_automaton.storage.models import Base

BROKER_ID = UUID("00000000-0000-4000-8000-000000000102")
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
SCALES = (100, 1_000, 10_000, 50_000)
AUTOMATION_COUNTS = (2, 100)
REPEATS = 7


def identifier(kind: str, index: int) -> str:
    return str(uuid5(BROKER_ID, f"{kind}:{index}"))


def seed(path: Path, intent_count: int, automation_count: int) -> tuple[str, ...]:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    automation_ids = tuple(identifier(f"automation:{automation_count}", i) for i in range(automation_count))
    automation_rows = [
        {
            "automation_id": automation_id,
            "broker_id": str(BROKER_ID),
            "account_id": "profile-account",
            "instrument_id": f"TICKER{i % 20}",
            "fact_instrument_id": identifier("instrument", i % 20),
            "lot_size": 1 + i % 10,
            "state": "IN_WORK",
            "revision": 1,
            "last_sequence_number": 0,
        }
        for i, automation_id in enumerate(automation_ids)
    ]
    intent_table = Base.metadata.tables["broker_intents"]
    decision_table = Base.metadata.tables["trade_decisions"]
    decisions: list[dict[str, object]] = []
    outbox_table = Base.metadata.tables["fact_outbox"]
    outbox_rows: list[dict[str, object]] = []
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["cached_automations"].insert(), automation_rows)
        intent_batch: list[dict[str, object]] = []
        for i in range(intent_count):
            automation_index = i % automation_count
            automation_id = automation_ids[automation_index]
            is_current = i < automation_count
            intent_id = identifier(f"intent:{automation_count}", i)
            state = "SUBMITTED" if is_current else ("FAILED" if i % 17 == 0 else "FILLED")
            timestamp = NOW - timedelta(seconds=intent_count - i)
            snapshot: dict[str, object] = {
                "admission_origin": {"runtime_instance_id": identifier("runtime", 1), "generation": 1}
            }
            terminal_at = None if is_current or state == "FAILED" and i % 34 == 0 else timestamp
            if state == "FAILED" and terminal_at is None:
                snapshot["_age_policy"] = {
                    "version": 1,
                    "expired_at": (timestamp + timedelta(hours=25)).isoformat(timespec="milliseconds"),
                }
            intent_batch.append(
                {
                    "idempotency_key": intent_id,
                    "fact_execution_id": None,
                    "position_cycle_id": None,
                    "automation_id": automation_id,
                    "kind": "BUY_MORE",
                    "side": "BUY",
                    "state": state,
                    "quantity_lots": 1,
                    "limit_price": "10.000000000",
                    "broker_order_id": None,
                    "requested_amount": "10.000000000",
                    "executed_amount": "0.000000000",
                    "estimated_commission": "0.100000000",
                    "executed_commission": "0.000000000",
                    "executed_lots": 0,
                    "executed_price": "0.000000000",
                    "execution_currency": "RUB",
                    "executed_at": None,
                    "dispatch_started_at": timestamp if is_current else None,
                    "broker_responded_at": None,
                    "terminal_at": terminal_at,
                    "execution_facts_emitted_at": None,
                    "strategy_snapshot": snapshot,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            if i % 10 == 0:
                decisions.append(
                    {
                        "id": identifier(f"decision:{automation_count}", i),
                        "process_id": identifier("process", i),
                        "automation_id": automation_id,
                        "broker_id": str(BROKER_ID),
                        "instrument_id": f"TICKER{automation_index % 20}",
                        "occurred_at": timestamp,
                        "quantity_lots": 1,
                        "lot_size": 1,
                        "average_price": "10.000000000",
                        "current_price": "10.000000000",
                        "best_bid": "9.990000000",
                        "best_ask": "10.010000000",
                        "invested_amount": "10.000000000",
                        "estimated_commission": "0.100000000",
                        "decision": "WAIT",
                        "reason_code": "PROFILE",
                        "decision_quantity_lots": 1,
                        "limit_price": "10.000000000",
                        "strategy_snapshot": {},
                        "intent_id": intent_id,
                    }
                )
            if len(intent_batch) == 2_000:
                connection.execute(intent_table.insert(), intent_batch)
                intent_batch.clear()
        if intent_batch:
            connection.execute(intent_table.insert(), intent_batch)
        if decisions:
            connection.execute(decision_table.insert(), decisions)
        for i, automation_id in enumerate(automation_ids):
            outbox_rows.append(
                {
                    "event_id": identifier(f"outbox:{automation_count}", i),
                    "user_broker_id": identifier("user-broker", 1),
                    "automation_id": automation_id,
                    "sequence_number": 1,
                    "expected_revision": 0,
                    "fact_kind": "PROFILE",
                    "payload": {},
                    "safe_message": "synthetic diagnostic profile",
                    "occurred_at": NOW,
                    "delivery_state": "PENDING" if i % 3 else "FAILED",
                    "retry_count": 0,
                    "next_retry_at": NOW,
                }
            )
        if outbox_rows:
            connection.execute(outbox_table.insert(), outbox_rows)
    engine.dispose()
    return automation_ids


def query_parameters(automation_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "automation_ids": automation_ids,
        "active_states": _ACTIVE_STATES,
        "terminal_states": _TERMINAL,
        "prod_terminal_unfinalized": True,
        "test_filled_unemitted": False,
        "captured_at": NOW,
    }


def query_plan(engine, statement, parameters: dict[str, object]) -> list[str]:
    compiled = statement.params(**parameters).compile(engine, compile_kwargs={"render_postcompile": True})
    values = tuple(compiled.params[key] for key in compiled.positiontup or ())
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + compiled.string, values).all()
    return [str(row[-1]) for row in rows]


def sql_median_ms(engine, statement, parameters: dict[str, object]) -> float:
    samples: list[float] = []
    with engine.connect() as connection:
        for _ in range(REPEATS):
            start = monotonic()
            result = connection.execute(statement, parameters).all()
            samples.append((monotonic() - start) * 1000)
            assert result is not None
    return statistics.median(samples)


def main() -> None:
    reports: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="moex-d1-profile-") as directory:
        for automation_count in AUTOMATION_COUNTS:
            for intent_count in SCALES:
                path = Path(directory) / f"{automation_count}-automations-{intent_count}-intents.sqlite"
                automation_ids = seed(path, intent_count, automation_count)
                engine = create_engine(f"sqlite:///{path}")
                parameters = query_parameters(automation_ids)
                statements = {
                    "unscoped_cache": (_UNSCOPED_CACHE, {}),
                    "automations": (_AUTOMATIONS, {"broker_id": str(BROKER_ID)}),
                    "intents": (_INTENTS, parameters),
                    "fact_counts": (_FACT_COUNTS, {"automation_ids": automation_ids}),
                }
                plans = {name: query_plan(engine, statement, values) for name, (statement, values) in statements.items()}
                medians = {
                    name: round(sql_median_ms(engine, statement, values), 3)
                    for name, (statement, values) in statements.items()
                }
                reader = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="PROD")
                full_samples: list[float] = []
                full_result = None
                for _ in range(REPEATS):
                    start = monotonic()
                    full_result = reader.read(BROKER_ID, NOW)
                    full_samples.append((monotonic() - start) * 1000)
                assert full_result is not None
                reports.append(
                    {
                        "automations": automation_count,
                        "total_intents": intent_count,
                        "intents_per_automation_mean": round(intent_count / automation_count, 1),
                        "query_median_ms": medians,
                        "full_read_median_ms": round(statistics.median(full_samples), 3),
                        "full_read_observation": full_result.observation,
                        "full_read_reason": full_result.reason,
                        "full_read_rows": len(full_result.rows),
                        "plans": plans,
                    }
                )
                engine.dispose()
    print(json.dumps({"synthetic_only": True, "repeats": REPEATS, "profiles": reports}, indent=2))


if __name__ == "__main__":
    main()
