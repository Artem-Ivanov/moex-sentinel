"""Behavior tests for user-broker persistence."""

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.user_brokers import (
    UserBrokerConstraintError,
    UserBrokerDraft,
    UserBrokerDuplicateError,
    UserBrokerNotFoundError,
    UserBrokerState,
)
from moex_sentinel.storage.database import create_session_factory
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from tests.storage.test_position_adoption_repository import SCOPE_ID, candidate, repository


@pytest.fixture
def database(core_engine: Engine) -> tuple[Engine, sessionmaker[Session]]:
    """Create a session factory; each repository operation owns its session."""
    return core_engine, create_session_factory(core_engine)


def draft(
    name: str = "Primary sandbox",
    *,
    account_id: str | None = "synthetic-account",
    state: UserBrokerState = UserBrokerState.ACTIVE,
    connection_value: str = "synthetic-token",
) -> UserBrokerDraft:
    return UserBrokerDraft(
        api_slug="t_invest",
        display_name=name,
        environment="TEST",
        fqdn="sandbox-invest-public-api.tbank.ru:443",
        settings={"token": connection_value},
        external_account_id=account_id,
        state=state,
    )


def test_repository_returns_detached_user_broker_records_in_creation_order(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    repository = UserBrokerRepository(factory)

    first = repository.create(draft(), record_id="scope-1")
    second = repository.create(draft("Draft", account_id=None, state=UserBrokerState.DRAFT), record_id="scope-2")

    assert repository.list() == [first, second]
    assert repository.get(first.id) == first
    assert first.settings == {"token": "synthetic-token"}


def test_duplicate_account_maps_to_safe_domain_error(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    repository = UserBrokerRepository(factory)
    repository.create(draft(), record_id="scope-1")

    with pytest.raises(UserBrokerDuplicateError) as caught:
        repository.create(draft("Duplicate", connection_value="never-echo"), record_id="scope-2")

    assert "never-echo" not in str(caught.value)


def test_replace_is_atomic_and_disable_preserves_configuration(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    repository = UserBrokerRepository(factory)
    created = repository.create(draft(), record_id="scope-1")

    replaced = repository.replace(
        created.id,
        draft("Renamed", connection_value="replacement-token"),
    )
    disabled = repository.disable(created.id)

    assert replaced.display_name == "Renamed"
    assert replaced.settings == {"token": "replacement-token"}
    assert disabled.state is UserBrokerState.DISABLED
    assert disabled.settings == replaced.settings
    assert disabled.external_account_id == replaced.external_account_id


def test_missing_user_broker_maps_to_not_found(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    repository = UserBrokerRepository(factory)

    with pytest.raises(UserBrokerNotFoundError):
        repository.get("missing")
    with pytest.raises(UserBrokerNotFoundError):
        repository.replace("missing", draft())
    with pytest.raises(UserBrokerNotFoundError):
        repository.disable("missing")


@pytest.mark.parametrize(
    "changes",
    [{"environment": "PROD"}, {"fqdn": "foreign"}, {"external_account_id": "foreign"}, {"api_slug": "foreign"}],
)
def test_scope_cannot_change_after_position_bootstrap(changes):
    adoption, factory = repository()
    try:
        adoption.adopt(candidate())
        repo = UserBrokerRepository(factory)
        current = repo.get(SCOPE_ID)
        value = UserBrokerDraft(**current.model_dump(exclude={"id", "created_at", "updated_at"}))
        with pytest.raises(UserBrokerConstraintError, match="immutable"):
            repo.replace(SCOPE_ID, value.model_copy(update=changes))
        assert repo.get(SCOPE_ID) == current
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.parametrize("disabled", [True, False])
def test_selected_scope_change_is_rejected_before_delayed_old_snapshot_adoption(disabled):
    adoption, factory = repository()
    try:
        repo = UserBrokerRepository(factory)
        old_snapshot = candidate()
        current = repo.disable(SCOPE_ID) if disabled else repo.get(SCOPE_ID)
        value = UserBrokerDraft(**current.model_dump(exclude={"id", "created_at", "updated_at"}))
        with pytest.raises(UserBrokerConstraintError, match="immutable"):
            repo.replace(SCOPE_ID, value.model_copy(update={"external_account_id": "account-B"}))
        adoption.adopt(old_snapshot)
        assert repo.get(SCOPE_ID).external_account_id == "account-1"
    finally:
        factory.kw["bind"].dispose()
