"""Scoped automation list projections with a constant database-read budget."""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.environment import PinnedEnvironment
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories import RecordNotFoundError
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from tests.storage.test_core_fact_lineage_reads import capture_statements
from tests.storage.trading_facts_helpers import (
    NOW,
    automation_model,
    instrument_model,
    position_cycle_model,
    user_broker_model,
)


@pytest.fixture
def read_model_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def read_model_factory(read_model_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(read_model_engine, expire_on_commit=False)


def seed_automations(factory: sessionmaker[Session], count: int = 50) -> list[str]:
    ids = [f"automation-{index:03d}" for index in range(count)]
    with factory.begin() as session:
        for index in range(10):
            broker = user_broker_model(f"broker-{index}", f"account-{index}")
            broker.environment = "TEST" if index % 2 == 0 else "PROD"
            if index == 0:
                broker.archived_at = NOW
                broker.state = "DISABLED"
            session.add(broker)
        session.flush()
        for index in range(count):
            session.add(instrument_model(f"instrument-{index:03d}", f"broker-{index % 10}"))
        session.flush()
        for index, automation_id in enumerate(ids):
            model = automation_model(
                automation_id, user_broker_id=f"broker-{index % 10}", instrument_id=f"instrument-{index:03d}"
            )
            model.created_at = NOW + timedelta(seconds=index // 3)
            if index == 0:
                model.state = "HOLD"
                model.bootstrap_position_cycle_id = "bootstrap-cycle"
                model.bootstrap_position_lot_id = "bootstrap-lot"
                model.bootstrap_quantity_lots = 2
                model.bootstrap_average_price = Decimal("10")
                model.bootstrap_invested_amount = Decimal("20")
                model.bootstrap_currency = "RUB"
                model.bootstrap_observed_at = NOW
            session.add(model)
        session.flush()
        if count > 1:
            cycle = position_cycle_model("open-cycle")
            cycle.user_broker_id = "broker-1"
            cycle.automation_id = ids[1]
            cycle.instrument_id = "instrument-001"
            cycle.quantity_lots = 2
            cycle.average_entry_price = Decimal("12.34")
            cycle.invested_amount = Decimal("24.68")
            cycle.realized_pnl = Decimal("1.2")
            cycle.unrealized_pnl = Decimal("2.3")
            cycle.net_pnl = Decimal("3.5")
            cycle.accumulated_commissions = Decimal("0.04")
            session.add(cycle)
        if count > 2:
            cycle = position_cycle_model("closed-cycle", state="CLOSED", closed=True)
            cycle.user_broker_id = "broker-2"
            cycle.automation_id = ids[2]
            cycle.instrument_id = "instrument-002"
            session.add(cycle)
    return ids


class TestAutomationReadModel:
    @pytest.mark.parametrize(
        ("method", "count"), [(method, count) for method in ("get_many", "list_active") for count in (0, 1, 50)]
    )
    def test_constant_sql_budget(self, read_model_factory, read_model_engine, method, count):
        ids = seed_automations(read_model_factory, count)
        repository = AutomationRepository(read_model_factory)
        with capture_statements(read_model_engine) as statements:
            result = repository.get_many(ids) if method == "get_many" else repository.list_active()
        assert [value.id for value in result] == ids
        assert statements == ([] if method == "get_many" and count == 0 else ["SELECT"])

    @pytest.mark.parametrize("method", ["get_many", "list_active"])
    def test_full_dto_matches_existing_single_record_projection(self, read_model_factory, method):
        ids = seed_automations(read_model_factory)
        repository = AutomationRepository(read_model_factory)
        expected = [repository.get(automation_id) for automation_id in ids]
        result = repository.get_many(list(reversed(ids))) if method == "get_many" else repository.list_active()
        assert result == expected
        assert result[0].bootstrap_pending is True
        assert result[0].quantity_lots == 0
        assert result[1].quantity_lots == 2
        assert (
            result[1].average_price,
            result[1].invested_amount,
            result[1].realized_pnl,
            result[1].unrealized_pnl,
            result[1].net_pnl,
            result[1].actual_commissions,
        ) == tuple(map(Decimal, ("12.34", "24.68", "1.2", "2.3", "3.5", "0.04")))
        assert result[2].quantity_lots == 0

    def test_ids_are_deduplicated_unknown_ids_ignored_and_order_is_created_then_id(self, read_model_factory):
        ids = seed_automations(read_model_factory, 3)
        repository = AutomationRepository(read_model_factory)
        assert [value.id for value in repository.get_many([ids[2], "absent", ids[0], ids[2], ids[1]])] == ids
        assert repository.get_many(["absent"]) == []

    def test_closed_automation_is_available_by_id_but_excluded_from_active_list(self, read_model_factory):
        ids = seed_automations(read_model_factory, 1)
        with read_model_factory.begin() as session:
            session.add(
                automation_model(
                    "closed", user_broker_id="broker-0", instrument_id="instrument-000", state="CLOSED", closed=True
                )
            )
        repository = AutomationRepository(read_model_factory)
        assert [value.id for value in repository.list_active()] == ids
        assert repository.get_many(["closed"])[0].state.value == "CLOSED"

    @pytest.mark.parametrize(
        ("method", "count"), [(method, count) for method in ("get_many", "list_active") for count in (1, 50)]
    )
    def test_service_total_sql_budget(self, read_model_factory, read_model_engine, method, count):
        ids = seed_automations(read_model_factory, count)
        service = AutomationService(
            AutomationRepository(read_model_factory),
            brokers=UserBrokerRepository(read_model_factory),
            environment=PinnedEnvironment("TEST"),
        )
        with capture_statements(read_model_engine) as statements:
            result = service.get_many(ids) if method == "get_many" else service.list_active()
        assert [value.id for value in result] == [item for index, item in enumerate(ids) if index % 2 == 0]
        assert statements == ["SELECT", "SELECT"]

    @pytest.mark.parametrize(("method", "ids"), [("get_many", []), ("get_many", ["absent"]), ("list_active", None)])
    def test_empty_service_result_avoids_broker_query(self, read_model_factory, read_model_engine, method, ids):
        seed_automations(read_model_factory, 0)
        service = AutomationService(
            AutomationRepository(read_model_factory),
            brokers=UserBrokerRepository(read_model_factory),
            environment=PinnedEnvironment("TEST"),
        )
        with capture_statements(read_model_engine) as statements:
            result = service.get_many(ids) if method == "get_many" else service.list_active()
        assert result == []
        assert statements == ([] if ids == [] else ["SELECT"])


@pytest.mark.parametrize(
    ("method", "corruption"),
    [
        (method, corruption)
        for method in ("get_many", "list_active")
        for corruption in ("missing_broker", "missing_instrument", "foreign_instrument")
    ],
)
def test_corrupt_scoped_links_raise_instead_of_hiding_automation(
    read_model_factory, read_model_engine, method, corruption
):
    ids = seed_automations(read_model_factory, 1)
    # Deliberately corrupt only this disposable SQLite database. Production FKs stay enabled.
    with read_model_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            if corruption == "missing_broker":
                connection.exec_driver_sql("UPDATE trading_automations SET user_broker_id = 'absent'")
            elif corruption == "missing_instrument":
                connection.exec_driver_sql("UPDATE trading_automations SET instrument_id = 'absent'")
            else:
                connection.exec_driver_sql("UPDATE broker_instruments SET user_broker_id = 'broker-1'")
            connection.commit()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    repository = AutomationRepository(read_model_factory)
    reader = repository.get_many if method == "get_many" else repository.list_active
    arguments = (ids,) if method == "get_many" else ()
    with pytest.raises(RecordNotFoundError, match="Automation scope is incomplete"):
        reader(*arguments)
