"""Current UserBroker read capabilities preserve archive and immutable scope."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from moex_sentinel.domain.user_brokers import UserBrokerConstraintError, UserBrokerDraft, UserBrokerState
from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
from moex_sentinel.services.portfolio import PortfolioAggregationService
from moex_sentinel.services.ports import UserBrokerLookupPort, UserBrokerReadPort
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


def test_current_read_ports_preserve_archived_identity_and_exclude_portfolio_reads():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        repository = UserBrokerRepository(sessionmaker(engine, expire_on_commit=False))
        record = repository.create(
            UserBrokerDraft(
                api_slug="t_invest",
                display_name="Test",
                environment="TEST",
                fqdn="sandbox-invest-public-api.tbank.ru:443",
                settings={"token": "synthetic"},
                external_account_id="account",
                state=UserBrokerState.ACTIVE,
            )
        )
        lookup: UserBrokerLookupPort = repository
        reader: UserBrokerReadPort = repository
        with pytest.raises(UserBrokerConstraintError, match="immutable"):
            repository.replace(
                record.id,
                UserBrokerDraft(
                    **{
                        **record.model_dump(exclude={"id", "created_at", "updated_at", "archived_at"}),
                        "external_account_id": "foreign",
                    }
                ),
            )
        repository.archive(record.id)
        scope = AutomatonBrokerService(lookup).scope(record.id)
        assert scope.account_id == "account"
        assert scope.environment == "TEST"
        assert reader.get(record.id).archived_at is not None

        def forbidden_adapter(_broker):
            raise AssertionError("Archived broker must not trigger portfolio I/O")

        result = asyncio.run(PortfolioAggregationService(reader, forbidden_adapter).view_all_accounts())
        assert result.accounts == ()
        assert result.errors == ()
    finally:
        engine.dispose()
