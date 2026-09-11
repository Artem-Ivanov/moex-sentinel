"""Behavior tests for the user-broker-scoped instrument catalog."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.instrument_catalog import (
    CatalogInstrumentNotFoundError,
    UserBrokerCatalogInstrumentDraft,
)
from moex_sentinel.domain.user_brokers import UserBrokerDraft, UserBrokerState
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


@pytest.fixture
def database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield engine, factory
    engine.dispose()


def user_broker_draft(account_id: str) -> UserBrokerDraft:
    return UserBrokerDraft(
        api_slug="t_invest",
        display_name=account_id,
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": "synthetic-token"},
        external_account_id=account_id,
        state=UserBrokerState.ACTIVE,
    )


def instrument(uid: str, ticker: str, *, name: str | None = None) -> UserBrokerCatalogInstrumentDraft:
    return UserBrokerCatalogInstrumentDraft(
        external_instrument_id=uid,
        external_identifiers={"figi": f"figi-{uid}"},
        ticker=ticker,
        name=name or ticker,
        instrument_type="SHARE",
        class_code="TQBR",
        currency="RUB",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        api_trade_available=True,
    )


def test_reconcile_is_isolated_and_preserves_selection(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    brokers = UserBrokerRepository(factory)
    first = brokers.create(user_broker_draft("account-1"), record_id="scope-1")
    second = brokers.create(user_broker_draft("account-2"), record_id="scope-2")
    repository = ReferenceCatalogRepository(factory)
    first_sync = datetime(2026, 8, 10, 10, tzinfo=UTC)
    second_sync = datetime(2026, 8, 10, 11, tzinfo=UTC)

    created = repository.reconcile(
        first.id,
        (instrument("uid-1", "ONE"), instrument("uid-2", "TWO")),
        first_sync,
    )
    repository.reconcile(second.id, (instrument("uid-1", "ONE"),), first_sync)
    selected = repository.list(first.id, include_inactive=True)[0]
    repository.set_selected(first.id, selected.id, True)
    changed = repository.reconcile(
        first.id,
        (instrument("uid-1", "ONE", name="Renamed"),),
        second_sync,
    )

    first_items = repository.list(first.id, include_inactive=True)
    second_items = repository.list(second.id, include_inactive=True)

    assert (created.added, created.updated, created.deactivated) == (2, 0, 0)
    assert (changed.added, changed.updated, changed.deactivated) == (0, 1, 1)
    assert first_items[0].name == "Renamed"
    assert first_items[0].last_seen_at == second_sync
    assert first_items[0].is_selected is True
    assert first_items[1].is_active is False
    assert second_items[0].is_active is True
    assert repository.list(first.id, include_inactive=False) == (first_items[0],)
    assert repository.sync_state(first.id).last_success_at == second_sync


def test_catalog_get_and_selection_are_scoped_by_user_broker(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    brokers = UserBrokerRepository(factory)
    first = brokers.create(user_broker_draft("account-1"), record_id="scope-1")
    second = brokers.create(user_broker_draft("account-2"), record_id="scope-2")
    repository = ReferenceCatalogRepository(factory)
    synchronized_at = datetime(2026, 8, 10, 10, tzinfo=UTC)
    repository.reconcile(first.id, (instrument("uid-1", "ONE"),), synchronized_at)
    repository.reconcile(second.id, (instrument("uid-1", "ONE"),), synchronized_at)
    first_instrument = repository.list(first.id, include_inactive=True)[0]

    assert repository.get(first.id, first_instrument.id) == first_instrument

    with pytest.raises(CatalogInstrumentNotFoundError):
        repository.get(second.id, first_instrument.id)
    with pytest.raises(CatalogInstrumentNotFoundError):
        repository.set_selected(second.id, first_instrument.id, True)


def test_catalog_finds_external_instrument_only_inside_requested_scope(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    brokers = UserBrokerRepository(factory)
    first = brokers.create(user_broker_draft("account-1"), record_id="scope-1")
    second = brokers.create(user_broker_draft("account-2"), record_id="scope-2")
    repository = ReferenceCatalogRepository(factory)
    synchronized_at = datetime(2026, 8, 10, 10, tzinfo=UTC)
    repository.reconcile(first.id, (instrument("shared-uid", "FIRST"),), synchronized_at)
    repository.reconcile(second.id, (instrument("shared-uid", "SECOND"),), synchronized_at)

    first_record = repository.find_by_external_instrument_id(first.id, "shared-uid")
    second_record = repository.find_by_external_instrument_id(second.id, "shared-uid")

    assert first_record is not None
    assert first_record.ticker == "FIRST"
    assert second_record is not None
    assert second_record.ticker == "SECOND"
    assert repository.find_by_external_instrument_id(first.id, "missing-uid") is None
