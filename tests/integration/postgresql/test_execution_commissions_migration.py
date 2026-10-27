"""The fee schema upgrades cleanly and refuses to drop recorded financial evidence."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from moex_sentinel.storage.models.execution_commissions import BrokerOrderFeeObservationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order


def config(database_url: URL) -> Config:
    value = Config("alembic.ini")
    value.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False).replace("%", "%%"))
    return value


@pytest.mark.postgresql
def test_empty_fee_schema_downgrades_and_upgrades(isolated_postgresql_database_url: URL) -> None:
    migration = config(isolated_postgresql_database_url)
    engine = create_engine(isolated_postgresql_database_url)
    try:
        command.downgrade(migration, "0007_portfolio_run_index")
        assert "broker_order_fee_observations" not in inspect(engine).get_table_names()
        command.upgrade(migration, "head")
        assert "broker_order_fee_observations" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_nonempty_fee_history_blocks_downgrade(isolated_postgresql_database_url: URL) -> None:
    migration = config(isolated_postgresql_database_url)
    engine = create_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            seed_order(session)
            broker = session.get(UserBrokerModel, "scope-1")
            broker.environment = "PROD"
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
                    scan_pass_id=str(uuid4()),
                    operation_id="fee-1",
                    amount=Decimal("1.72"),
                    status="KNOWN",
                    occurred_at=NOW,
                    observed_at=NOW,
                )
            )
        with pytest.raises(RuntimeError, match="financial history"):
            command.downgrade(migration, "0007_portfolio_run_index")
        with factory() as session:
            assert session.scalar(select(BrokerOrderFeeObservationModel.id)) is not None
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_concurrent_fee_writer_is_serialized_before_downgrade_count(
    isolated_postgresql_database_url: URL,
) -> None:
    """The downgrade lock must make its emptiness check see a concurrent committed fee."""
    migration = config(isolated_postgresql_database_url)
    engine = create_engine(isolated_postgresql_database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    started = Event()
    try:
        with factory.begin() as session:
            seed_order(session)
        writer = engine.connect()
        transaction = writer.begin()
        writer.execute(
            BrokerOrderFeeObservationModel.__table__.insert().values(
                id=str(uuid4()),
                user_broker_id="scope-1",
                broker_order_id="order-1",
                parent_operation_id="exchange-order-1",
                operation_type="BROKER_FEE",
                currency="RUB",
                scan_pass_id=str(uuid4()),
                operation_id="concurrent-fee",
                amount=Decimal("1.72"),
                status="KNOWN",
                occurred_at=NOW,
                observed_at=NOW,
            )
        )

        def downgrade() -> None:
            started.set()
            command.downgrade(migration, "0007_portfolio_run_index")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(downgrade)
            assert started.wait(timeout=5)
            deadline = monotonic() + 5
            waiting_for_lock = False
            while monotonic() < deadline:
                with engine.connect() as observer:
                    waiting_for_lock = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                                "AND query ILIKE '%LOCK TABLE broker_order_fee_observations%')"
                            )
                        )
                    )
                if waiting_for_lock:
                    break
                sleep(0.02)
            # Prove ACCESS EXCLUSIVE reached PostgreSQL and is blocked by the INSERT transaction.
            assert waiting_for_lock
            transaction.commit()
            writer.close()
            with pytest.raises(RuntimeError, match="financial history"):
                future.result(timeout=10)
        with factory() as session:
            assert session.scalar(select(BrokerOrderFeeObservationModel.id)) is not None
    finally:
        if not writer.closed:
            transaction.rollback()
            writer.close()
        engine.dispose()
