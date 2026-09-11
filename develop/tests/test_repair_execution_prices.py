"""Offline recovery changes only proven unit errors and reproducible projections."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import JSON, Column, Integer, MetaData, Numeric, String, Table, create_engine, select

from develop.scripts.repair_execution_prices import RepairRefused, allocation_money, repair_databases

D = Decimal


def rows():
    worker = {
        "worker_runs": [{"worker_id": "worker", "clean_shutdown": 1}],
        "fact_outbox": [],
        "cached_automations": [
            {
                "automation_id": "a",
                "lot_size": 10,
                "state": "IN_WORK",
                "revision": 1,
                "last_sequence_number": 6,
                "user_broker_id": "b",
                "fact_instrument_id": "i",
            }
        ],
        "broker_intents": [
            {
                "idempotency_key": "buy",
                "automation_id": "a",
                "fact_execution_id": "ebuy",
                "position_cycle_id": "c",
                "side": "BUY",
                "state": "FILLED",
                "executed_lots": 1,
                "executed_price": D("1100"),
                "executed_amount": D("1100"),
                "executed_commission": D("1"),
                "limit_price": D("111"),
            },
            {
                "idempotency_key": "sell",
                "automation_id": "a",
                "fact_execution_id": "esell",
                "position_cycle_id": "c",
                "side": "SELL",
                "state": "FILLED",
                "executed_lots": 2,
                "executed_price": D("2400"),
                "executed_amount": D("2400"),
                "executed_commission": D("2"),
                "limit_price": D("119"),
            },
        ],
        "trade_lots": [
            {
                "id": "bootstrap",
                "automation_id": "a",
                "source_intent_id": None,
                "source": "BROKER_POSITION_BOOTSTRAP",
                "original_lots": 2,
                "remaining_lots": 1,
                "entry_price": D("100"),
                "entry_commission": D("0"),
                "opened_at": "2026-01-01",
            },
            {
                "id": "bought",
                "automation_id": "a",
                "source_intent_id": "buy",
                "source": "EXECUTED",
                "original_lots": 1,
                "remaining_lots": 0,
                "entry_price": D("1100"),
                "entry_commission": D("1"),
                "opened_at": "2026-01-02",
            },
        ],
        "lot_allocations": [
            {
                "id": "alloc-buy",
                "automation_id": "a",
                "sell_intent_id": "sell",
                "lot_id": "bought",
                "quantity_lots": 1,
                "exit_price": D("2400"),
                "exit_commission": D("1"),
                "realized_pnl": D("12998"),
            },
            {
                "id": "alloc-bootstrap",
                "automation_id": "a",
                "sell_intent_id": "sell",
                "lot_id": "bootstrap",
                "quantity_lots": 1,
                "exit_price": D("2400"),
                "exit_commission": D("1"),
                "realized_pnl": D("22999"),
            },
        ],
        "trading_cycle_states": [{"automation_id": "a", "last_sell_price": D("2400")}],
    }
    core = {
        "trading_automations": [
            {
                "id": "a",
                "state": "IN_WORK",
                "revision": 1,
                "last_sequence_number": 6,
                "user_broker_id": "b",
                "instrument_id": "i",
            }
        ],
        "broker_orders": [
            {
                "id": r["idempotency_key"],
                "automation_id": "a",
                "state": "FILLED",
                "executed_amount": r["executed_amount"],
                "executed_commission": r["executed_commission"],
            }
            for r in worker["broker_intents"]
        ],
        "trade_executions": [
            {
                "id": r["fact_execution_id"],
                "broker_order_id": r["idempotency_key"],
                "automation_id": "a",
                "user_broker_id": "b",
                "instrument_id": "i",
                "position_cycle_id": "c",
                "side": r["side"],
                "executed_lots": r["executed_lots"],
                "price": r["executed_price"],
                "value": r["executed_amount"],
                "broker_commission": r["executed_commission"],
                "other_fees": D("0"),
            }
            for r in worker["broker_intents"]
        ],
        "position_lots": [
            {
                **{k: v for k, v in r.items() if k not in {"source_intent_id", "source"}},
                "position_cycle_id": "c",
                "buy_execution_id": "ebuy" if r["source_intent_id"] else None,
                "source": "BROKER_EXECUTION" if r["source_intent_id"] else "BROKER_POSITION_BOOTSTRAP",
                "user_broker_id": "b",
            }
            for r in worker["trade_lots"]
        ],
        "execution_lot_allocations": [
            {
                "id": r["id"],
                "automation_id": "a",
                "sell_execution_id": "esell",
                "position_lot_id": r["lot_id"],
                "allocated_lots": 1,
                "entry_value": D("11000") if r["lot_id"] == "bought" else D("1000"),
                "exit_value": D("24000"),
                "entry_commission": D("1") if r["lot_id"] == "bought" else D("0"),
                "exit_commission": D("1"),
                "realized_pnl": r["realized_pnl"],
            }
            for r in worker["lot_allocations"]
        ],
        "position_cycles": [
            {
                "id": "c",
                "automation_id": "a",
                "quantity_lots": 1,
                "average_entry_price": D("100"),
                "invested_amount": D("1000"),
                "realized_pnl": D("35997"),
                "unrealized_pnl": D("200"),
                "net_pnl": D("36197"),
                "accumulated_commissions": D("3"),
            }
        ],
        "automation_events": [
            {
                "event_id": "e1",
                "automation_id": "a",
                "sequence_number": 1,
                "fact_kind": "POSITION_LOT_OPENED",
                "payload": {"position_lot_id": "bootstrap", "source": "BROKER_POSITION_BOOTSTRAP"},
            },
            {
                "event_id": "e2",
                "automation_id": "a",
                "sequence_number": 2,
                "fact_kind": "TRADE_EXECUTION_RECORDED",
                "payload": {"execution_id": "ebuy", "price": "1100"},
            },
            {
                "event_id": "e3",
                "automation_id": "a",
                "sequence_number": 3,
                "fact_kind": "TRADE_EXECUTION_RECORDED",
                "payload": {"execution_id": "esell", "price": "2400"},
            },
        ],
    }
    return worker, core


def make_engine(path, data):
    engine = create_engine(f"sqlite:///{path}")
    metadata = MetaData()
    with engine.begin() as connection:
        for name, records in data.items():
            if not records:
                Table(name, metadata, Column("id", String, primary_key=True)).create(connection)
                continue
            keys = set().union(*(record.keys() for record in records))
            columns = []
            for key in sorted(keys):
                sample = next((r[key] for r in records if r.get(key) is not None), None)
                kind = (
                    Numeric(28, 9)
                    if isinstance(sample, Decimal)
                    else JSON if isinstance(sample, dict) else Integer if isinstance(sample, int) else String
                )
                columns.append(Column(key, kind))
            table = Table(name, metadata, *columns)
            table.create(connection)
            connection.execute(table.insert(), records)
    return engine


def load(engine, name):
    with engine.connect() as connection:
        table = Table(name, MetaData(), autoload_with=connection)
        return [dict(row) for row in connection.execute(select(table)).mappings()]


def test_dry_run_then_repair_corrects_graph_preserves_envelopes_and_is_idempotent(tmp_path):
    w, c = rows()
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    dry = repair_databases(worker, core)
    assert dry["affected_executions"] == 2
    assert load(worker, "broker_intents")[0]["executed_price"] == D("1100")
    repaired = repair_databases(worker, core, apply=True)
    assert repaired["affected_executions"] == 2
    assert [r["executed_price"] for r in load(worker, "broker_intents")] == [D("110"), D("120")]
    assert [r["realized_pnl"] for r in load(worker, "lot_allocations")] == [D("98"), D("199")]
    cycle = load(core, "position_cycles")[0]
    assert cycle["realized_pnl"] == D("297")
    assert cycle["unrealized_pnl"] == D("200")
    assert cycle["net_pnl"] == D("497")
    assert load(core, "automation_events") == c["automation_events"]
    assert repair_databases(worker, core, apply=True)["affected_executions"] == 0


def test_repair_retry_after_core_commit_is_safe(tmp_path):
    w, c = rows()
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)

    def crash():
        raise RuntimeError("Synthetic lost connection after Core commit")

    with pytest.raises(RuntimeError):
        repair_databases(worker, core, apply=True, after_core_commit=crash)
    assert load(core, "trade_executions")[0]["price"] == D("110")
    assert load(worker, "broker_intents")[0]["executed_price"] == D("1100")
    assert repair_databases(worker, core, apply=True)["affected_executions"] == 2
    assert load(worker, "broker_intents")[0]["executed_price"] == D("110")


@pytest.mark.parametrize("breakage", ["running", "active_intent", "wrong_price", "topology", "scope"])
def test_unproven_or_live_graph_is_refused_without_changes(tmp_path, breakage):
    w, c = rows()
    if breakage == "running":
        w["worker_runs"][0]["clean_shutdown"] = 0
    elif breakage == "active_intent":
        w["broker_intents"][0]["state"] = "UNCERTAIN"
    elif breakage == "wrong_price":
        w["broker_intents"][0]["executed_price"] = D("300")
    elif breakage == "topology":
        w["lot_allocations"][0]["quantity_lots"] = 2
    else:
        c["trade_executions"][0]["user_broker_id"] = "different"
    original = deepcopy(w)
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    with pytest.raises(RepairRefused):
        repair_databases(worker, core, apply=True)
    assert load(worker, "broker_intents") == original["broker_intents"]


def test_partial_lot_keeps_only_remaining_entry_fee_in_net(tmp_path):
    w, c = rows()
    buy, sell = w["broker_intents"]
    buy.update(executed_lots=2, executed_price=D("2200"), executed_amount=D("2200"), executed_commission=D("2"))
    sell.update(executed_lots=1, executed_price=D("1200"), executed_amount=D("1200"), executed_commission=D("1"))
    w["trade_lots"][0]["remaining_lots"] = 2
    w["trade_lots"][1].update(original_lots=2, remaining_lots=1, entry_price=D("2200"), entry_commission=D("2"))
    w["lot_allocations"] = w["lot_allocations"][:1]
    w["lot_allocations"][0].update(exit_price=D("1200"), realized_pnl=D("-10002"))
    w["trading_cycle_states"][0]["last_sell_price"] = D("1200")
    for index, intent in enumerate(w["broker_intents"]):
        c["trade_executions"][index].update(
            executed_lots=intent["executed_lots"],
            price=intent["executed_price"],
            value=intent["executed_amount"],
            broker_commission=intent["executed_commission"],
        )
        c["broker_orders"][index].update(
            executed_amount=intent["executed_amount"], executed_commission=intent["executed_commission"]
        )
    c["position_lots"][0]["remaining_lots"] = 2
    c["position_lots"][1].update(original_lots=2, remaining_lots=1, entry_price=D("2200"), entry_commission=D("2"))
    c["execution_lot_allocations"] = c["execution_lot_allocations"][:1]
    c["execution_lot_allocations"][0].update(entry_value=D("22000"), exit_value=D("12000"), realized_pnl=D("-10002"))
    c["position_cycles"][0].update(
        quantity_lots=3,
        average_entry_price=D("800"),
        invested_amount=D("24000"),
        realized_pnl=D("-10002"),
        unrealized_pnl=D("-20400"),
        net_pnl=D("-30402"),
    )
    c["automation_events"][1]["payload"]["price"] = "2200"
    c["automation_events"][2]["payload"]["price"] = "1200"
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    repair_databases(worker, core, apply=True)
    snapshot = load(core, "position_cycles")[0]
    assert snapshot["invested_amount"] == D("3100")
    assert snapshot["realized_pnl"] == D("98")
    assert snapshot["unrealized_pnl"] == D("500")
    assert snapshot["net_pnl"] == D("597")
    assert snapshot["accumulated_commissions"] == D("3")


def test_irrelevant_audit_history_does_not_consume_graph_bound(tmp_path):
    w, c = rows()
    c["automation_events"].extend(
        {
            "event_id": f"audit-{number}",
            "automation_id": "a",
            "sequence_number": number + 100,
            "fact_kind": "TRADE_AUDIT_RECORDED",
            "payload": {"synthetic": "irrelevant"},
        }
        for number in range(100)
    )
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    assert repair_databases(worker, core, max_rows=10)["affected_executions"] == 2


def test_queued_fact_refuses_mutation(tmp_path):
    w, c = rows()
    w["fact_outbox"] = [{"id": "queued"}]
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    with pytest.raises(RepairRefused, match="WORKER_OUTBOX_NOT_EMPTY"):
        repair_databases(worker, core, apply=True)


def test_new_graph_member_after_core_commit_refuses_resume(tmp_path):
    w, c = rows()
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)

    def crash():
        raise RuntimeError("Synthetic interruption")

    with pytest.raises(RuntimeError):
        repair_databases(worker, core, apply=True, after_core_commit=crash)
    with core.begin() as connection:
        target = Table("automation_events", MetaData(), autoload_with=connection)
        connection.execute(
            target.insert().values(
                event_id="new-fill",
                automation_id="a",
                sequence_number=999,
                fact_kind="TRADE_EXECUTION_RECORDED",
                payload={"execution_id": "unknown", "price": "10"},
            )
        )
    with pytest.raises(RepairRefused):
        repair_databases(worker, core, apply=True)
    assert load(worker, "broker_intents")[0]["executed_price"] == D("1100")


def test_repair_preserves_latest_market_mark_instead_of_last_fill_price(tmp_path):
    w, c = rows()
    # Current quote is 115, while the last SELL fill normalized to 120.
    c["position_cycles"][0].update(unrealized_pnl=D("150"), net_pnl=D("36147"))
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    repair_databases(worker, core, apply=True)
    snapshot = load(core, "position_cycles")[0]
    assert snapshot["unrealized_pnl"] == D("150")
    assert snapshot["net_pnl"] == D("447")


def test_fractional_allocation_fees_round_once_at_persisted_boundaries():
    result = allocation_money(
        entry_price=D("10"),
        exit_price=D("10"),
        quantity=1,
        lot_size=10,
        entry_commission=D("1"),
        original_lots=3,
        exit_commission=D("1"),
        executed_lots=3,
    )
    assert result["entry_commission"] == D("0.333333333")
    assert result["exit_commission"] == D("0.333333333")
    assert result["realized_pnl"] == D("-0.666666667")


def test_ambiguous_terminal_total_as_market_mark_refuses_repair(tmp_path):
    w, c = rows()
    c["position_cycles"][0].update(unrealized_pnl=D("23000"), net_pnl=D("58997"))
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    with pytest.raises(RepairRefused, match="CURRENT_MARK_OUTSIDE_EXECUTION_BOUND"):
        repair_databases(worker, core, apply=True)


def test_fully_closed_cycle_has_zero_gross_and_no_remaining_entry_fee(tmp_path):
    w, c = rows()
    w["broker_intents"][1].update(
        executed_lots=3, executed_price=D("3600"), executed_amount=D("3600"), executed_commission=D("3")
    )
    w["trade_lots"][0]["remaining_lots"] = 0
    w["lot_allocations"][0].update(exit_price=D("3600"), realized_pnl=D("24998"))
    w["lot_allocations"][1].update(
        quantity_lots=2, exit_price=D("3600"), exit_commission=D("2"), realized_pnl=D("69998")
    )
    w["trading_cycle_states"][0]["last_sell_price"] = D("3600")
    c["trade_executions"][1].update(executed_lots=3, price=D("3600"), value=D("3600"), broker_commission=D("3"))
    c["broker_orders"][1].update(executed_amount=D("3600"), executed_commission=D("3"))
    c["position_lots"][0]["remaining_lots"] = 0
    c["execution_lot_allocations"][0].update(exit_value=D("36000"), realized_pnl=D("24998"))
    c["execution_lot_allocations"][1].update(
        allocated_lots=2, entry_value=D("2000"), exit_value=D("72000"), exit_commission=D("2"), realized_pnl=D("69998")
    )
    c["position_cycles"][0].update(
        quantity_lots=0,
        average_entry_price=D("0"),
        invested_amount=D("0"),
        realized_pnl=D("94996"),
        unrealized_pnl=D("0"),
        net_pnl=D("94996"),
        accumulated_commissions=D("4"),
    )
    c["automation_events"][2]["payload"]["price"] = "3600"
    worker, core = make_engine(tmp_path / "worker.db", w), make_engine(tmp_path / "core.db", c)
    repair_databases(worker, core, apply=True)
    snapshot = load(core, "position_cycles")[0]
    assert snapshot["quantity_lots"] == 0
    assert snapshot["invested_amount"] == 0
    assert snapshot["unrealized_pnl"] == 0
    assert snapshot["net_pnl"] == D("496")
    assert snapshot["realized_pnl"] == D("496")
