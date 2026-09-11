"""Exercise recovery against migrated Core PostgreSQL and isolated Worker SQLite."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from develop.scripts.repair_execution_prices import repair_databases
from develop.tests.test_repair_execution_prices import D, load, make_engine, rows
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeDecisionModel, TradeExecutionModel
from moex_sentinel.storage.models.position_facts import ExecutionLotAllocationModel, PositionLotModel
from moex_sentinel.storage.models.trading_observability import AutomationEventModel
from tests.integration.postgresql.conftest import (  # noqa: F401
    isolated_postgresql_database_url,
    postgresql_database_url,
)
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

NOW = datetime(2026, 1, 3, tzinfo=UTC)


def seed_core(engine, core_rows):
    with Session(engine) as session, session.begin():
        session.add(user_broker_model("b", "synthetic-account"))
        session.flush()
        session.add(instrument_model("i", "b"))
        session.flush()
        session.add(
            TradingAutomationModel(
                **core_rows["trading_automations"][0], resume_requested=False, created_at=NOW, updated_at=NOW
            )
        )
        session.flush()
        session.add(
            PositionCycleModel(
                **core_rows["position_cycles"][0],
                user_broker_id="b",
                instrument_id="i",
                state="OPEN",
                opened_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        for row in core_rows["broker_orders"]:
            identifier = row["id"]
            session.add(
                TradeDecisionModel(
                    id=f"d-{identifier}",
                    fact_id=f"fd-{identifier}",
                    user_broker_id="b",
                    automation_id="a",
                    position_cycle_id="c",
                    instrument_id="i",
                    quantity_lots=1,
                    lot_size=10,
                    average_price=D("100"),
                    invested_amount=D("1000"),
                    current_price=D("110"),
                    best_bid=D("109"),
                    best_ask=D("110"),
                    indicators={},
                    estimated_commission=D("0"),
                    decision="BUY_MORE",
                    reason_code="SYNTHETIC",
                    requested_quantity_lots=1,
                    limit_price=D("111"),
                    strategy_snapshot={},
                    decided_at=NOW,
                    created_at=NOW,
                )
            )
            session.flush()
            session.add(
                BrokerOrderModel(
                    **row,
                    fact_id=f"fo-{identifier}",
                    user_broker_id="b",
                    decision_id=f"d-{identifier}",
                    position_cycle_id="c",
                    instrument_id="i",
                    idempotency_key=identifier,
                    intent_kind="BUY_MORE" if identifier == "buy" else "SELL_PART",
                    side="BUY" if identifier == "buy" else "SELL",
                    order_type="LIMIT",
                    quantity_lots=1 if identifier == "buy" else 2,
                    limit_price=D("111") if identifier == "buy" else D("119"),
                    requested_amount=row["executed_amount"],
                    estimated_commission=D("0"),
                    strategy_snapshot={},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        for row in core_rows["trade_executions"]:
            session.add(
                TradeExecutionModel(
                    **row,
                    fact_id=f"f-{row['id']}",
                    external_execution_id=f"x-{row['id']}",
                    currency="RUB",
                    source="BROKER_FILL",
                    executed_at=NOW,
                    created_at=NOW,
                )
            )
        session.flush()
        for row in core_rows["position_lots"]:
            values = {**row, "opened_at": datetime.fromisoformat(row["opened_at"]).replace(tzinfo=UTC)}
            session.add(PositionLotModel(**values, created_at=NOW, updated_at=NOW))
        session.flush()
        for row in core_rows["execution_lot_allocations"]:
            session.add(
                ExecutionLotAllocationModel(
                    **row, user_broker_id="b", position_cycle_id="c", allocated_at=NOW, created_at=NOW
                )
            )
        session.flush()
        for row in core_rows["automation_events"]:
            session.add(
                AutomationEventModel(
                    **row,
                    user_broker_id="b",
                    expected_revision=1,
                    safe_message="Synthetic execution history",
                    occurred_at=NOW,
                    received_at=NOW,
                )
            )


@pytest.mark.postgresql
def test_migrated_postgresql_repair_and_journal_resume(isolated_postgresql_database_url, tmp_path):  # noqa: F811
    worker_rows, core_rows = rows()
    worker = make_engine(tmp_path / "worker.db", worker_rows)
    core = create_engine(isolated_postgresql_database_url, hide_parameters=True)
    try:
        seed_core(core, core_rows)
        before = load(core, "automation_events")
        assert repair_databases(worker, core)["affected_executions"] == 2

        def interrupt():
            raise RuntimeError("Synthetic lost Core acknowledgement")

        with pytest.raises(RuntimeError):
            repair_databases(worker, core, apply=True, after_core_commit=interrupt)
        assert load(core, "trade_executions")[0]["price"] == D("110")
        assert load(worker, "broker_intents")[0]["executed_price"] == D("1100")
        assert repair_databases(worker, core, apply=True)["affected_executions"] == 2
        assert load(core, "position_cycles")[0]["net_pnl"] == D("497")
        assert load(core, "automation_events") == before
        assert repair_databases(worker, core, apply=True)["affected_executions"] == 0
    finally:
        worker.dispose()
        core.dispose()
