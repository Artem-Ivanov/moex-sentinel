"""Offline, bounded repair of confirmed total-as-unit execution prices.

Dry-run is the default. No broker API is imported or called. Accepted
``automation_events`` remain immutable evidence. Explicit correction journals
in both databases are the overlay required after any future projection rebuild.

Apply prepares a durable Worker journal, commits Core corrections and its journal,
then commits Worker corrections. A retry resumes the identical journal after a
lost Core acknowledgement; it never guesses from partially repaired projections.
Only IDs, checksums and necessary corrected projection fields enter the journals.
CLI output contains aggregate counts and fixed reason codes, never row payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import JSON, Column, MetaData, String, Table, and_, create_engine, inspect, or_, select, text
from sqlalchemy.engine import Connection, Engine

from trading_automaton.domain.position_valuation import net_position_pnl

JOURNAL = "execution_price_repair_journal"
SCALE = Decimal("0.000000001")
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
WORKER_TABLES = ("broker_intents", "trade_lots", "lot_allocations", "trading_cycle_states")
CORE_TABLES = (
    "trading_automations",
    "broker_orders",
    "trade_executions",
    "position_lots",
    "execution_lot_allocations",
    "position_cycles",
    "automation_events",
)
KEYS = {
    "worker_runs": "worker_id",
    "cached_automations": "automation_id",
    "broker_intents": "idempotency_key",
    "trading_cycle_states": "automation_id",
    "automation_events": "event_id",
}


class RepairRefused(RuntimeError):
    """Messages are fixed, non-sensitive reason codes."""


def decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise RepairRefused("NONFINITE_MONEY")
    return result.quantize(SCALE)


def canonical(value: object) -> object:
    if isinstance(value, (Decimal, float)):
        return str(decimal(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def table(connection: Connection, name: str) -> Table:
    return Table(name, MetaData(), autoload_with=connection)


def read(connection: Connection, name: str, maximum: int, ids: set[str] | None = None) -> list[dict[str, Any]]:
    target = table(connection, name)
    query = select(target)
    if name == "automation_events":
        query = query.where(
            or_(
                target.c.fact_kind == "TRADE_EXECUTION_RECORDED",
                and_(
                    target.c.fact_kind == "POSITION_LOT_OPENED",
                    target.c.payload["source"].as_string() == "BROKER_POSITION_BOOTSTRAP",
                ),
            )
        )
    if ids is not None:
        column = target.c.automation_id if "automation_id" in target.c else target.c.id
        query = query.where(column.in_(sorted(ids)))
    rows = [dict(row) for row in connection.execute(query.limit(maximum + 1)).mappings()]
    if len(rows) > maximum:
        raise RepairRefused("RECOVERY_BOUND_EXCEEDED")
    return rows


def stopped(worker: Connection) -> None:
    runs = read(worker, "worker_runs", 1000)
    if not runs or any(not row["clean_shutdown"] for row in runs):
        raise RepairRefused("WORKER_NOT_CLEANLY_STOPPED")
    if worker.execute(select(table(worker, "fact_outbox")).limit(1)).first() is not None:
        raise RepairRefused("WORKER_OUTBOX_NOT_EMPTY")
    intents = table(worker, "broker_intents")
    if worker.execute(select(intents).where(intents.c.state.not_in(TERMINAL)).limit(1)).first() is not None:
        raise RepairRefused("ACTIVE_OR_UNCERTAIN_INTENT")


def unit_price(fill: dict[str, Any], lot_size: int) -> tuple[Decimal, bool]:
    units = int(fill["executed_lots"]) * lot_size
    price, amount = decimal(fill["executed_price"]), decimal(fill["executed_amount"])
    if units <= 0 or amount <= 0 or price <= 0:
        raise RepairRefused("INVALID_EXECUTION_AMOUNTS")
    normalized = decimal(amount / units)
    if abs(price * units - amount) <= SCALE * units:
        return price, False
    limit = decimal(fill["limit_price"])
    if units <= 1 or price != amount or limit <= 0:
        raise RepairRefused("UNPROVEN_EXECUTION_PRICE")
    if abs(normalized - limit) / limit > Decimal("0.20"):
        raise RepairRefused("NORMALIZED_PRICE_OUTSIDE_BOUND")
    if (fill["side"] == "BUY" and normalized > limit + SCALE) or (
        fill["side"] == "SELL" and normalized < limit - SCALE
    ):
        raise RepairRefused("NORMALIZED_PRICE_VIOLATES_LIMIT")
    if fill["side"] not in {"BUY", "SELL"}:
        raise RepairRefused("UNKNOWN_EXECUTION_SIDE")
    return normalized, True


def indexed(rows: list[dict[str, Any]], key: str = "id") -> dict[str, dict[str, Any]]:
    output = {str(row[key]): row for row in rows}
    if len(output) != len(rows):
        raise RepairRefused("DUPLICATE_GRAPH_ID")
    return output


def allocation_money(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: int,
    lot_size: int,
    entry_commission: Decimal,
    original_lots: int,
    exit_commission: Decimal,
    executed_lots: int,
) -> dict[str, Decimal]:
    """Match runtime arithmetic: round persisted fields, never intermediate fees."""
    entry_fee = entry_commission * quantity / original_lots
    exit_fee = exit_commission * quantity / executed_lots
    return {
        "entry_value": decimal(entry_price * quantity * lot_size),
        "exit_value": decimal(exit_price * quantity * lot_size),
        "entry_commission": decimal(entry_fee),
        "exit_commission": decimal(exit_fee),
        "realized_pnl": decimal((exit_price - entry_price) * quantity * lot_size - entry_fee - exit_fee),
    }


def require_equal(first: object, second: object, reason: str) -> None:
    if canonical(first) != canonical(second):
        raise RepairRefused(reason)


def patch(plan: dict[str, Any], database: str, name: str, row: dict[str, Any], values: dict[str, Any]) -> None:
    values = {key: decimal(value) if isinstance(value, Decimal) else value for key, value in values.items()}
    changed = {key: value for key, value in values.items() if canonical(row[key]) != canonical(value)}
    if changed:
        key = KEYS.get(name, "id")
        plan["patches"].append(
            {
                "database": database,
                "table": name,
                "key_column": key,
                "key": row[key],
                "before": canonical({field: row[field] for field in changed}),
                "after": canonical(changed),
            }
        )


def replay_group(
    plan: dict[str, Any],
    cached: dict[str, Any],
    worker: dict[str, list[dict[str, Any]]],
    core: dict[str, list[dict[str, Any]]],
) -> None:
    automation_id = cached["automation_id"]
    lot_size = int(cached["lot_size"])
    automation = indexed(core["trading_automations"]).get(automation_id)
    if automation is None:
        raise RepairRefused("CORE_AUTOMATION_MISSING")
    for field in ("state", "revision", "last_sequence_number", "user_broker_id"):
        require_equal(cached[field], automation[field], "CORE_WORKER_AUTOMATION_CONFLICT")
    require_equal(cached["fact_instrument_id"], automation["instrument_id"], "INSTRUMENT_SCOPE_CONFLICT")
    fills = {
        key: value
        for key, value in indexed(worker["broker_intents"], "idempotency_key").items()
        if value["executed_lots"] > 0
    }
    executions = indexed(core["trade_executions"])
    require_equal(set(executions), {fill["fact_execution_id"] for fill in fills.values()}, "EXECUTION_GRAPH_INCOMPLETE")
    orders = indexed(core["broker_orders"])
    corrected: dict[str, Decimal] = {}
    for fill_id, fill in fills.items():
        execution = executions[fill["fact_execution_id"]]
        order = orders.get(fill_id)
        if order is None or order["state"] not in TERMINAL:
            raise RepairRefused("CORE_ORDER_NOT_TERMINAL")
        for worker_field, core_field in (
            ("executed_lots", "executed_lots"),
            ("executed_amount", "value"),
            ("executed_price", "price"),
            ("executed_commission", "broker_commission"),
            ("side", "side"),
            ("position_cycle_id", "position_cycle_id"),
        ):
            require_equal(fill[worker_field], execution[core_field], "CORE_WORKER_EXECUTION_CONFLICT")
        for field in ("executed_amount", "executed_commission"):
            require_equal(fill[field], order[field], "CORE_ORDER_AMOUNT_CONFLICT")
        if (
            execution["user_broker_id"] != cached["user_broker_id"]
            or execution["instrument_id"] != cached["fact_instrument_id"]
            or execution["broker_order_id"] != fill_id
            or decimal(execution["other_fees"]) != 0
        ):
            raise RepairRefused("EXECUTION_SCOPE_OR_FEES_CONFLICT")
        price, affected = unit_price(fill, lot_size)
        corrected[fill_id] = price
        plan["affected_executions"] += int(affected)
        patch(plan, "worker", "broker_intents", fill, {"executed_price": price})
        patch(plan, "core", "trade_executions", execution, {"price": price})
    lots = indexed(worker["trade_lots"])
    core_lots = indexed(core["position_lots"])
    require_equal(set(lots), set(core_lots), "LOT_GRAPH_INCOMPLETE")
    for lot_id, lot in lots.items():
        other = core_lots[lot_id]
        for field in ("original_lots", "remaining_lots", "entry_price", "entry_commission"):
            require_equal(lot[field], other[field], "CORE_WORKER_LOT_CONFLICT")
        if other["user_broker_id"] != cached["user_broker_id"]:
            raise RepairRefused("LOT_SCOPE_CONFLICT")
        if lot["source_intent_id"]:
            fill = fills.get(lot["source_intent_id"])
            if (
                fill is None
                or fill["side"] != "BUY"
                or other["buy_execution_id"] != fill["fact_execution_id"]
                or lot["original_lots"] != fill["executed_lots"]
                or decimal(lot["entry_commission"]) != decimal(fill["executed_commission"])
            ):
                raise RepairRefused("BUY_LOT_CONFLICT")
            price = corrected[lot["source_intent_id"]]
            patch(plan, "worker", "trade_lots", lot, {"entry_price": price})
            patch(plan, "core", "position_lots", other, {"entry_price": price})
        elif lot["source"] != "BROKER_POSITION_BOOTSTRAP" or other["source"] != "BROKER_POSITION_BOOTSTRAP":
            raise RepairRefused("UNPROVEN_BOOTSTRAP_LOT")
    allocations = indexed(worker["lot_allocations"])
    core_allocations = indexed(core["execution_lot_allocations"])
    require_equal(set(allocations), set(core_allocations), "ALLOCATION_GRAPH_INCOMPLETE")
    for allocation_id, allocation in allocations.items():
        other = core_allocations[allocation_id]
        for worker_field, core_field in (
            ("lot_id", "position_lot_id"),
            ("quantity_lots", "allocated_lots"),
            ("exit_commission", "exit_commission"),
            ("realized_pnl", "realized_pnl"),
        ):
            require_equal(allocation[worker_field], other[core_field], "CORE_WORKER_ALLOCATION_CONFLICT")
        if (
            allocation["sell_intent_id"] not in fills
            or fills[allocation["sell_intent_id"]]["fact_execution_id"] != other["sell_execution_id"]
        ):
            raise RepairRefused("ALLOCATION_SELL_CONFLICT")
    events = sorted(core["automation_events"], key=lambda row: row["sequence_number"])
    remaining = dict.fromkeys(lots, 0)
    seen_lots: set[str] = set()
    seen_fills: set[str] = set()
    seen_allocations: set[str] = set()
    realized = Decimal()
    fees = Decimal()
    cycle_snapshots: dict[str, dict[str, Any]] = {}
    cycle_limits: dict[str, Decimal] = {}
    last_sell = None
    for event in events:
        payload = event["payload"]
        if event["fact_kind"] == "POSITION_LOT_OPENED" and payload.get("source") == "BROKER_POSITION_BOOTSTRAP":
            lot_id = payload["position_lot_id"]
            if lot_id not in lots or lot_id in seen_lots or lots[lot_id]["source_intent_id"] is not None:
                raise RepairRefused("BOOTSTRAP_EVENT_CONFLICT")
            remaining[lot_id] = int(lots[lot_id]["original_lots"])
            seen_lots.add(lot_id)
            fees += decimal(lots[lot_id]["entry_commission"])
            continue
        if event["fact_kind"] != "TRADE_EXECUTION_RECORDED":
            continue
        execution_id = payload["execution_id"]
        execution = executions.get(execution_id)
        if execution is None or execution_id in seen_fills:
            raise RepairRefused("EXECUTION_EVENT_CONFLICT")
        fill_id = execution["broker_order_id"]
        fill, price = fills[fill_id], corrected[fill_id]
        require_equal(decimal(payload["price"]), decimal(fill["executed_price"]), "ACCEPTED_EXECUTION_PRICE_CONFLICT")
        seen_fills.add(execution_id)
        if fill["side"] == "BUY":
            bought = [lot_id for lot_id, lot in lots.items() if lot["source_intent_id"] == fill_id]
            if len(bought) != 1 or bought[0] in seen_lots:
                raise RepairRefused("BUY_LOT_EVENT_CONFLICT")
            remaining[bought[0]] = int(fill["executed_lots"])
            seen_lots.add(bought[0])
            fees += decimal(fill["executed_commission"])
        else:
            to_sell = int(fill["executed_lots"])
            lifo = sorted(
                (lot_id for lot_id in seen_lots if remaining[lot_id] > 0),
                key=lambda key: (str(lots[key]["opened_at"]), key),
                reverse=True,
            )
            for lot_id in lifo:
                if to_sell == 0:
                    break
                quantity = min(to_sell, remaining[lot_id])
                matches = [
                    row for row in allocations.values() if row["sell_intent_id"] == fill_id and row["lot_id"] == lot_id
                ]
                if len(matches) != 1 or int(matches[0]["quantity_lots"]) != quantity:
                    raise RepairRefused("LIFO_ALLOCATION_TOPOLOGY_CONFLICT")
                allocation = matches[0]
                seen_allocations.add(allocation["id"])
                lot = lots[lot_id]
                entry_price = (
                    corrected[lot["source_intent_id"]] if lot["source_intent_id"] else decimal(lot["entry_price"])
                )
                money = allocation_money(
                    entry_price=entry_price,
                    exit_price=price,
                    quantity=quantity,
                    lot_size=lot_size,
                    entry_commission=decimal(lot["entry_commission"]),
                    original_lots=int(lot["original_lots"]),
                    exit_commission=decimal(fill["executed_commission"]),
                    executed_lots=int(fill["executed_lots"]),
                )
                require_equal(
                    decimal(allocation["exit_commission"]), money["exit_commission"], "ALLOCATION_FEE_CONFLICT"
                )
                pnl = money["realized_pnl"]
                realized += pnl
                patch(plan, "worker", "lot_allocations", allocation, {"exit_price": price, "realized_pnl": pnl})
                patch(
                    plan,
                    "core",
                    "execution_lot_allocations",
                    core_allocations[allocation["id"]],
                    money,
                )
                remaining[lot_id] -= quantity
                to_sell -= quantity
            if to_sell:
                raise RepairRefused("SELL_EXCEEDS_REPLAY_INVENTORY")
            fees += decimal(fill["executed_commission"])
            last_sell = fill_id
        quantity = sum(remaining.values())
        invested = sum(
            (
                (
                    corrected[lots[key]["source_intent_id"]]
                    if lots[key]["source_intent_id"]
                    else decimal(lots[key]["entry_price"])
                )
                * count
                * lot_size
                for key, count in remaining.items()
            ),
            Decimal(),
        )
        average = invested / (quantity * lot_size) if quantity else Decimal()
        unrealized = price * quantity * lot_size - invested
        commission_lots = (
            SimpleNamespace(
                entry_commission=decimal(lots[key]["entry_commission"]),
                remaining_lots=count,
                original_lots=int(lots[key]["original_lots"]),
            )
            for key, count in remaining.items()
        )
        cycle_snapshots[fill["position_cycle_id"]] = {
            "quantity_lots": quantity,
            "average_entry_price": decimal(average),
            "invested_amount": decimal(invested),
            "realized_pnl": decimal(realized),
            "unrealized_pnl": decimal(unrealized),
            "net_pnl": decimal(net_position_pnl(realized, unrealized, commission_lots)),
            "accumulated_commissions": decimal(fees),
        }
        cycle_limits[fill["position_cycle_id"]] = decimal(fill["limit_price"])
    require_equal(seen_fills, set(executions), "MISSING_EXECUTION_EVENTS")
    require_equal(seen_lots, set(lots), "MISSING_LOT_EVENTS")
    require_equal(seen_allocations, set(allocations), "UNREPLAYED_ALLOCATION")
    for lot_id, lot in lots.items():
        require_equal(remaining[lot_id], lot["remaining_lots"], "REMAINING_QUANTITY_CONFLICT")
    for cycle in core["position_cycles"]:
        if cycle["id"] not in cycle_snapshots:
            raise RepairRefused("CYCLE_WITHOUT_REPLAY_VALUATION")
        snapshot = cycle_snapshots[cycle["id"]]
        require_equal(cycle["quantity_lots"], snapshot["quantity_lots"], "CYCLE_QUANTITY_CONFLICT")
        require_equal(
            decimal(cycle["accumulated_commissions"]), snapshot["accumulated_commissions"], "CYCLE_COMMISSION_CONFLICT"
        )
        # Preserve the market mark and valuation timestamp already recorded by
        # Core. A later tick may have valued this position after the last fill.
        old_market_value = decimal(cycle["unrealized_pnl"]) + decimal(cycle["invested_amount"])
        if snapshot["quantity_lots"]:
            mark = old_market_value / (snapshot["quantity_lots"] * lot_size)
            if not mark.is_finite() or mark <= 0:
                raise RepairRefused("CURRENT_MARK_UNAVAILABLE")
            limit = cycle_limits[cycle["id"]]
            if limit <= 0 or abs(mark - limit) / limit > Decimal("0.20"):
                raise RepairRefused("CURRENT_MARK_OUTSIDE_EXECUTION_BOUND")
            unrealized = decimal(old_market_value - snapshot["invested_amount"])
        else:
            unrealized = Decimal()
        snapshot["net_pnl"] = decimal(snapshot["net_pnl"] + unrealized - snapshot["unrealized_pnl"])
        snapshot["unrealized_pnl"] = unrealized
        patch(plan, "core", "position_cycles", cycle, snapshot)
    for state in worker["trading_cycle_states"]:
        if last_sell and state["last_sell_price"] is not None:
            require_equal(
                decimal(state["last_sell_price"]),
                decimal(fills[last_sell]["executed_price"]),
                "LAST_SELL_PRICE_CONFLICT",
            )
            patch(plan, "worker", "trading_cycle_states", state, {"last_sell_price": corrected[last_sell]})


def build_plan(worker: Connection, core: Connection, maximum: int) -> dict[str, Any]:
    stopped(worker)
    caches = indexed(read(worker, "cached_automations", maximum), "automation_id")
    intents = read(worker, "broker_intents", maximum)
    affected: set[str] = set()
    for fill in intents:
        if fill["executed_lots"] <= 0:
            continue
        cached = caches.get(fill["automation_id"])
        if cached is None or not cached["lot_size"] or cached["lot_size"] <= 0:
            raise RepairRefused("LOT_SIZE_UNAVAILABLE")
        _, bad = unit_price(fill, int(cached["lot_size"]))
        if bad:
            affected.add(fill["automation_id"])
    plan: dict[str, Any] = {
        "version": 1,
        "groups": len(affected),
        "affected_executions": 0,
        "patches": [],
        "guards": [],
        "sets": [],
    }
    if not affected:
        plan["plan_id"] = digest(plan)
        return plan
    data = {
        "worker": {name: read(worker, name, maximum, affected) for name in WORKER_TABLES},
        "core": {name: read(core, name, maximum, affected) for name in CORE_TABLES},
    }
    data["worker"]["cached_automations"] = [caches[key] for key in sorted(affected)]
    for automation_id in sorted(affected):
        scoped = {
            database: {
                name: [row for row in records if row.get("automation_id", row.get("id")) == automation_id]
                for name, records in tables.items()
            }
            for database, tables in data.items()
        }
        replay_group(plan, caches[automation_id], scoped["worker"], scoped["core"])
    changed = {(item["database"], item["table"], item["key"]): set(item["after"]) for item in plan["patches"]}
    for database, tables in data.items():
        for name, records in tables.items():
            key = KEYS.get(name, "id")
            plan["sets"].append(
                {
                    "database": database,
                    "table": name,
                    "key_column": key,
                    "automation_ids": sorted(affected),
                    "keys": sorted(row[key] for row in records),
                }
            )
            for row in records:
                columns = sorted(set(row) - changed.get((database, name, row[key]), set()))
                plan["guards"].append(
                    {
                        "database": database,
                        "table": name,
                        "key_column": key,
                        "key": row[key],
                        "columns": columns,
                        "hash": digest({column: row[column] for column in columns}),
                    }
                )
    plan["plan_id"] = digest(plan)
    return plan


def journal(connection: Connection) -> Table:
    target = Table(
        JOURNAL,
        MetaData(),
        Column("plan_id", String(64), primary_key=True),
        Column("status", String(16), nullable=False),
        Column("plan", JSON, nullable=False),
        Column("created_at", String(64), nullable=False),
    )
    return target


def pending_plan(worker: Connection) -> dict[str, Any] | None:
    if not inspect(worker).has_table(JOURNAL):
        return None
    target = journal(worker)
    rows = worker.execute(select(target).where(target.c.status == "PREPARED").limit(2)).mappings().all()
    if len(rows) > 1:
        raise RepairRefused("MULTIPLE_PENDING_REPAIRS")
    return dict(rows[0]["plan"]) if rows else None


def verify_plan(plan: dict[str, Any], worker: Connection, core: Connection) -> None:
    require_equal(
        plan["plan_id"],
        digest({key: value for key, value in plan.items() if key != "plan_id"}),
        "REPAIR_JOURNAL_CHECKSUM_CONFLICT",
    )
    stopped(worker)
    connections = {"worker": worker, "core": core}
    for collection in plan["sets"]:
        current = read(
            connections[collection["database"]],
            collection["table"],
            len(collection["keys"]),
            set(collection["automation_ids"]),
        )
        require_equal(
            sorted(row[collection["key_column"]] for row in current), collection["keys"], "REPAIR_GRAPH_MEMBERS_CHANGED"
        )
    for guard in plan["guards"]:
        connection = connections[guard["database"]]
        target = table(connection, guard["table"])
        row = (
            connection.execute(select(target).where(target.c[guard["key_column"]] == guard["key"]))
            .mappings()
            .one_or_none()
        )
        if row is None or digest({field: row[field] for field in guard["columns"]}) != guard["hash"]:
            raise RepairRefused("REPAIR_GRAPH_CHANGED")
    for item in plan["patches"]:
        connection = connections[item["database"]]
        target = table(connection, item["table"])
        row = (
            connection.execute(select(target).where(target.c[item["key_column"]] == item["key"]))
            .mappings()
            .one_or_none()
        )
        actual = None if row is None else canonical({field: row[field] for field in item["after"]})
        if actual not in (item["before"], item["after"]):
            raise RepairRefused("REPAIR_PROJECTION_CHANGED")


def apply_patches(connection: Connection, database: str, plan: dict[str, Any]) -> None:
    for item in plan["patches"]:
        if item["database"] != database:
            continue
        target = table(connection, item["table"])
        values = {
            field: (Decimal(value) if isinstance(value, str) and target.c[field].type.python_type is Decimal else value)
            for field, value in item["after"].items()
        }
        result = connection.execute(target.update().where(target.c[item["key_column"]] == item["key"]).values(**values))
        if result.rowcount != 1:
            raise RepairRefused("REPAIR_ROW_COUNT_CONFLICT")


def repair_databases(
    worker_engine: Engine,
    core_engine: Engine,
    *,
    apply: bool = False,
    max_rows: int = 10000,
    after_core_commit: Callable[[], None] | None = None,
) -> dict[str, int]:
    if max_rows <= 0 or worker_engine.dialect.name != "sqlite":
        raise RepairRefused("INVALID_RECOVERY_CONFIGURATION")
    with localcontext() as context, worker_engine.connect() as worker, core_engine.connect() as core:
        context.prec = 50
        worker.exec_driver_sql("BEGIN IMMEDIATE" if apply else "BEGIN")
        core.begin()
        if core.dialect.name == "postgresql":
            if apply:
                core.execute(text("LOCK TABLE " + ", ".join(CORE_TABLES) + " IN SHARE ROW EXCLUSIVE MODE"))
            else:
                core.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        plan = pending_plan(worker) or build_plan(worker, core, max_rows)
        verify_plan(plan, worker, core)
        report = {
            "groups": plan["groups"],
            "affected_executions": plan["affected_executions"],
            "worker_changes": sum(item["database"] == "worker" for item in plan["patches"]),
            "core_changes": sum(item["database"] == "core" for item in plan["patches"]),
        }
        if not apply or not plan["patches"]:
            return report
        worker_journal = journal(worker)
        worker_journal.create(worker, checkfirst=True)
        existing = worker.execute(select(worker_journal).where(worker_journal.c.plan_id == plan["plan_id"])).first()
        if existing is None:
            worker.execute(
                worker_journal.insert().values(
                    plan_id=plan["plan_id"], status="PREPARED", plan=plan, created_at=datetime.now(UTC).isoformat()
                )
            )
        worker.commit()
        worker.exec_driver_sql("BEGIN IMMEDIATE")
        verify_plan(plan, worker, core)
        core_journal = journal(core)
        core_journal.create(core, checkfirst=True)
        existing = (
            core.execute(select(core_journal).where(core_journal.c.plan_id == plan["plan_id"])).mappings().one_or_none()
        )
        if existing is not None:
            require_equal(existing["plan"], plan, "CORE_REPAIR_JOURNAL_CONFLICT")
        apply_patches(core, "core", plan)
        if existing is None:
            core.execute(
                core_journal.insert().values(
                    plan_id=plan["plan_id"], status="COMPLETE", plan=plan, created_at=datetime.now(UTC).isoformat()
                )
            )
        core.commit()
        if after_core_commit is not None:
            after_core_commit()
        apply_patches(worker, "worker", plan)
        worker.execute(
            worker_journal.update().where(worker_journal.c.plan_id == plan["plan_id"]).values(status="COMPLETE")
        )
        worker.commit()
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-database", type=Path, required=True)
    parser.add_argument("--core-database-url-env", default="DATABASE_URL")
    parser.add_argument("--max-rows", type=int, default=10000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    worker_engine = core_engine = None
    try:
        path = args.worker_database.resolve(strict=True)
        mode = "rw" if args.apply else "ro"
        worker_engine = create_engine(
            "sqlite://", creator=lambda: sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)
        )
        core_engine = create_engine(os.environ[args.core_database_url_env], echo=False, hide_parameters=True)
        report = repair_databases(worker_engine, core_engine, apply=args.apply, max_rows=args.max_rows)
        sys.stdout.write(json.dumps({"mode": "apply" if args.apply else "dry-run", **report}) + "\n")
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, RepairRefused) else type(error).__name__
        sys.stdout.write(json.dumps({"error": code}) + "\n")
        return 1
    finally:
        if worker_engine is not None:
            worker_engine.dispose()
        if core_engine is not None:
            core_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
