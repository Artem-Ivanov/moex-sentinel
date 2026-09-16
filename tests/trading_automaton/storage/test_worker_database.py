"""Worker SQLite connection policy."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Column, MetaData, Table, inspect, select

from trading_automaton.storage import database
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base, TradeDecisionModel, UTCDateTime


def test_worker_sqlite_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
    engine.dispose()


def test_worker_engine_rejects_postgresql_url() -> None:
    with pytest.raises(ValueError, match="private SQLite"):
        create_worker_engine("postgresql+psycopg://database/worker")


def test_worker_datetime_floors_persisted_values_to_milliseconds(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    metadata = MetaData()
    moments = Table("moments", metadata, Column("value", UTCDateTime(), nullable=False))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(moments.insert().values(value=datetime(2026, 8, 13, 10, 0, 0, 123001, tzinfo=UTC)))
        restored = connection.execute(select(moments.c.value)).scalar_one()

    assert restored == datetime(2026, 8, 13, 10, 0, 0, 123000, tzinfo=UTC)
    engine.dispose()


def test_clean_worker_schema_has_one_typed_fact_outbox(tmp_path) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'worker-baseline.db'}")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert "fact_outbox" in tables
    assert "fact_outbox_v2" not in tables
    assert "outbox_events" not in tables
    engine.dispose()


@pytest.mark.parametrize("existing", [pytest.param(False, id="fresh"), pytest.param(True, id="existing-with-rows")])
def test_worker_schema_adds_decision_lookup_index_without_changing_rows(tmp_path, existing) -> None:
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'upgrade.sqlite'}")
    try:
        if existing:
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP INDEX IF EXISTS ix_trade_decisions_intent_id")
                values = {
                    "id": "decision-1",
                    "automation_id": "automation-1",
                    "broker_id": "broker-1",
                    "instrument_id": "instrument-1",
                    "occurred_at": datetime(2026, 9, 16, tzinfo=UTC),
                    "quantity_lots": 1,
                    "lot_size": 10,
                    "average_price": Decimal("100.123456789"),
                    "current_price": Decimal("101"),
                    "best_bid": Decimal("101"),
                    "best_ask": Decimal("102"),
                    "invested_amount": Decimal("1001.234567890"),
                    "estimated_commission": Decimal("0.125"),
                    "decision": "BUY_MORE",
                    "reason_code": "TEST",
                    "decision_quantity_lots": 1,
                    "strategy_snapshot": {"source": "synthetic"},
                    "intent_id": "intent-1",
                }
                connection.execute(TradeDecisionModel.__table__.insert().values(**values))
                before = connection.execute(select(TradeDecisionModel.__table__)).mappings().all()
        else:
            before = []

        database.initialize_worker_schema(engine)
        database.initialize_worker_schema(engine)

        index = next(
            item
            for item in inspect(engine).get_indexes("trade_decisions")
            if item["name"] == "ix_trade_decisions_intent_id"
        )
        assert index["column_names"] == ["intent_id"]
        assert not index["unique"]
        with engine.connect() as connection:
            assert connection.execute(select(TradeDecisionModel.__table__)).mappings().all() == before
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM trade_decisions WHERE intent_id = ?", ("intent-1",)
            ).all()
        assert any("SEARCH" in row[3] and "ix_trade_decisions_intent_id" in row[3] for row in plan)
    finally:
        engine.dispose()
