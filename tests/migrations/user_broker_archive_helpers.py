"""A populated reference/financial graph for archive and schema preservation tests."""

from decimal import Decimal

from sqlalchemy import MetaData, Table, select

from moex_sentinel.storage.models import (
    AutomationEventModel,
    Base,
    BrokerAccountFeeProfileModel,
    BrokerOrderEventModel,
    InstrumentSyncStateModel,
    PortfolioSnapshotModel,
    PortfolioSnapshotRunModel,
    PositionValuationSnapshotModel,
    TradeAuditEventModel,
)
from tests.storage.test_trading_facts_models import allocation_model, position_lot_model
from tests.storage.trading_facts_helpers import (
    NOW,
    automation_model,
    decision_model,
    execution_model,
    instrument_model,
    order_model,
    position_cycle_model,
    user_broker_model,
)


def seed_archive_graph(engine) -> None:
    """Insert through reflected tables so both revision 0002 and current metadata work."""
    sell_execution = execution_model("execution-sell", external_execution_id="external-sell", source="BROKER_FILL")
    sell_execution.side = "SELL"
    rows = [
        user_broker_model("scope-1", "account-1"),
        instrument_model("instrument-1", "scope-1"),
        InstrumentSyncStateModel(
            user_broker_id="scope-1",
            status="READY",
            last_attempt_at=NOW,
            last_success_at=NOW,
            safe_error=None,
            reconciliation_required=False,
            created_at=NOW,
            updated_at=NOW,
        ),
        automation_model("automation-1"),
        position_cycle_model("cycle-1"),
        decision_model(),
        order_model("order-1"),
        BrokerOrderEventModel(
            id="order-event-1",
            fact_id="fact-order-event-1",
            user_broker_id="scope-1",
            automation_id="automation-1",
            broker_order_id="order-1",
            from_state=None,
            to_state="CREATED",
            safe_reason="CREATED",
            safe_message="Synthetic event",
            occurred_at=NOW,
            created_at=NOW,
        ),
        execution_model("execution-buy", external_execution_id="external-buy", source="BROKER_FILL"),
        sell_execution,
        position_lot_model(),
        allocation_model("allocation-1"),
        AutomationEventModel(
            event_id="event-1",
            automation_id="automation-1",
            user_broker_id="scope-1",
            sequence_number=1,
            expected_revision=1,
            fact_kind="TRADE_DECISION",
            safe_message="Synthetic",
            payload={"preserved": True},
            occurred_at=NOW,
            received_at=NOW,
        ),
        TradeAuditEventModel(
            event_id="audit-1",
            process_id="process-1",
            parent_process_id=None,
            user_broker_id="scope-1",
            automation_id="automation-1",
            decision_id="decision-1",
            broker_order_id="order-1",
            execution_id="execution-buy",
            instrument_id="instrument-1",
            level="INFO",
            stage="SYNTHETIC",
            safe_message="Synthetic audit",
            data={"preserved": True},
            occurred_at=NOW,
            created_at=NOW,
            critical=False,
        ),
        BrokerAccountFeeProfileModel(
            id="profile-1",
            user_broker_id="scope-1",
            instrument_type="SHARE",
            currency="RUB",
            buy_rate=Decimal("0.0005"),
            sell_rate=Decimal("0.0005"),
            service_rate=Decimal(),
            deal_rate=Decimal(),
            source="SYNTHETIC",
            calculated_at=NOW,
            valid_until=NOW,
            created_at=NOW,
            updated_at=NOW,
        ),
        PortfolioSnapshotRunModel(
            id="run-1",
            captured_at=NOW,
            bucket_start=NOW,
            safe_errors=[],
            created_at=NOW,
        ),
        PortfolioSnapshotModel(
            id="snapshot-1",
            run_id="run-1",
            user_broker_id="scope-1",
            account_id="account-1",
            total_value=Decimal("100"),
            free_cash=Decimal("25"),
            cumulative_pnl=Decimal("3"),
            currency="RUB",
            captured_at=NOW,
            bucket_start=NOW,
            created_at=NOW,
        ),
        PositionValuationSnapshotModel(
            id="valuation-1",
            user_broker_id="scope-1",
            automation_id="automation-1",
            position_cycle_id="cycle-1",
            instrument_id="instrument-1",
            quantity_lots=1,
            average_price=Decimal("10"),
            current_price=Decimal("9"),
            invested_amount=Decimal("10"),
            market_value=Decimal("9"),
            realized_pnl=Decimal("-1"),
            unrealized_pnl=Decimal("-1"),
            net_pnl=Decimal("-2"),
            actual_commissions=Decimal("0.01"),
            source="SYNTHETIC",
            captured_at=NOW,
            created_at=NOW,
        ),
    ]
    metadata = MetaData()
    with engine.begin() as connection:
        for row in rows:
            table = Table(row.__tablename__, metadata, autoload_with=connection, extend_existing=True)
            values = {column.name: getattr(row, column.name) for column in table.columns}
            connection.execute(table.insert().values(**values))


def archive_graph_snapshot(engine, *, include_archive: bool = True) -> dict[str, list[dict]]:
    """Compare complete row contents, including every table in the referenced graph."""
    metadata = MetaData()
    result = {}
    with engine.connect() as connection:
        for name in Base.metadata.tables:
            table = Table(name, metadata, autoload_with=connection)
            columns = [column for column in table.columns if include_archive or column.name != "archived_at"]
            result[name] = [
                dict(row) for row in connection.execute(select(*columns).order_by(*table.primary_key)).mappings()
            ]
    return result
