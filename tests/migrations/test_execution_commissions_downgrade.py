"""SQLite downgrade preserves the fee journal and serializes concurrent writers."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.models.execution_commissions import BrokerOrderFeeObservationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order


def migration_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_sqlite_fee_downgrade_refuses_nonempty_financial_journal(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fee-history.db'}"
    config = migration_config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            seed_order(session)
            session.get(UserBrokerModel, "scope-1").environment = "PROD"
            order = session.get(BrokerOrderModel, "order-1")
            order.external_order_id = "exchange-order-1"
            order.state = "EXPIRED"
            order.executed_amount = Decimal("10.25")
            order.executed_commission = Decimal("0.01")
            order.terminal_at = NOW
            session.add(execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL"))
            session.add(
                BrokerOrderFeeObservationModel(
                    user_broker_id="scope-1",
                    broker_order_id="order-1",
                    parent_operation_id="exchange-order-1",
                    operation_type="BROKER_FEE",
                    currency="RUB",
                    scan_pass_id="pass-1",  # noqa: S106 - synthetic test identity, not a credential
                    operation_id="fee-op",
                    amount=Decimal("1.72"),
                    status="KNOWN",
                    occurred_at=NOW,
                    observed_at=NOW,
                )
            )
        with pytest.raises(RuntimeError, match="financial history"):
            command.downgrade(config, "0007_portfolio_run_index")
        assert "broker_order_fee_observations" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_sqlite_writer_cannot_insert_between_empty_check_and_fee_table_drop(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fee-downgrade-race.db'}"
    config = migration_config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    blocked: list[str] = []

    @event.listens_for(engine, "connect")
    def set_writer_timeout(connection, _record) -> None:
        connection.execute("PRAGMA busy_timeout=100")

    def concurrent_insert(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "DROP TABLE broker_fee_scan_state" not in statement:
            return
        try:
            with engine.begin() as writer:
                writer.execute(
                    text(
                        "INSERT INTO broker_order_fee_observations "
                        "(id, user_broker_id, broker_order_id, parent_operation_id, operation_type, currency, "
                        "scan_pass_id, operation_id, amount, status, occurred_at, observed_at) "
                        "VALUES ('fee-race', 'scope', 'order', 'parent', 'BROKER_FEE', 'RUB', 'pass', 'op', "
                        "1.0, 'KNOWN', '2026-01-01', '2026-01-01')"
                    )
                )
        except OperationalError as error:
            blocked.append(str(error.orig).lower())

    event.listen(Engine, "before_cursor_execute", concurrent_insert)
    try:
        command.downgrade(config, "0007_portfolio_run_index")
        assert len(blocked) == 1
        assert "locked" in blocked[0]
        assert "broker_order_fee_observations" not in inspect(engine).get_table_names()
    finally:
        event.remove(Engine, "before_cursor_execute", concurrent_insert)
        engine.dispose()
