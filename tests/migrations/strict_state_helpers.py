"""Behavioral fixtures shared by ORM and migrated schema checks."""

from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table
from sqlalchemy.exc import IntegrityError

from tests.storage.trading_facts_helpers import NOW, automation_model, instrument_model, user_broker_model

BOOTSTRAP = {
    "bootstrap_position_cycle_id": "bootstrap-cycle",
    "bootstrap_position_lot_id": "bootstrap-lot",
    "bootstrap_quantity_lots": 2,
    "bootstrap_average_price": 10,
    "bootstrap_invested_amount": 20,
    "bootstrap_currency": "RUB",
    "bootstrap_observed_at": NOW,
}
INVALID_BOOTSTRAP = [
    {**BOOTSTRAP, field: value}
    for field in BOOTSTRAP
    for value in (
        [None, 0, -1]
        if field in {"bootstrap_quantity_lots", "bootstrap_average_price", "bootstrap_invested_amount"}
        else [None]
    )
]
INVALID_RUNTIME = [
    ("OWNER", None, NOW),
    ("RETIRED", None, None),
    ("OWNER", 1, None),
    ("OWNER", 0, NOW),
    ("RETIRED", -1, None),
    ("PENDING", 1, None),
    ("PENDING", None, NOW),
    ("UNKNOWN", 1, NOW),
]
VALID_RUNTIME = [("PENDING", None, None), ("OWNER", 1, NOW), ("RETIRED", 1, None), ("RETIRED", 2, NOW)]


def insert_model(connection, model) -> None:
    table = Table(model.__tablename__, MetaData(), autoload_with=connection)
    values = {column.name: getattr(model, column.name) for column in table.columns}
    if "settings_revision" in values:
        values["settings_revision"] = 0
    connection.execute(table.insert().values(**values))


def seed_scope(engine) -> None:
    with engine.begin() as connection:
        insert_model(connection, user_broker_model("scope-1", "account-1"))
        insert_model(connection, instrument_model("instrument-1", "scope-1"))


def insert_bootstrap(engine, bootstrap) -> None:
    model = automation_model(str(uuid4()), state="CLOSED", closed=True)
    for field, value in bootstrap.items():
        setattr(model, field, value)
    with engine.begin() as connection:
        insert_model(connection, model)


def insert_runtime(engine, status, generation, lease) -> None:
    table = Table("worker_runtime_instances", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert().values(
                runtime_instance_id=str(uuid4()),
                user_broker_id="scope-1",
                worker_id="worker",
                environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
                secret_digest="a" * 64,
                registration_status=status,
                generation=generation,
                lease_expires_at=lease,
                registered_at=NOW,
                last_seen_at=NOW,
            )
        )


def assert_strict_insert_behavior(engine) -> None:
    insert_bootstrap(engine, {})
    insert_bootstrap(engine, BOOTSTRAP)
    for bootstrap in INVALID_BOOTSTRAP:
        with pytest.raises(IntegrityError, match="ck_trading_automations_complete_bootstrap_snapshot"):
            insert_bootstrap(engine, bootstrap)
    for runtime in VALID_RUNTIME:
        insert_runtime(engine, *runtime)
    for runtime in INVALID_RUNTIME:
        with pytest.raises(IntegrityError, match="ck_worker_runtime_"):
            insert_runtime(engine, *runtime)
