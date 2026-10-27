"""Account discovery must work before activating a broker connection."""

import asyncio
from decimal import Decimal

import pytest

from moex_sentinel.composition import build_application_usecases
from moex_sentinel.domain.brokers import BrokerDraft, BrokerField
from moex_sentinel.domain.portfolio import AccountPortfolio, BrokerAccount, Money
from moex_sentinel.domain.user_brokers import UserBrokerState
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.usecases.errors import UseCaseError


class SandboxPortfolio:
    def __init__(self, token: str, target: str) -> None:
        pass

    async def list_accounts(self):
        return (BrokerAccount("account-1", "Sandbox", "ACCOUNT_STATUS_OPEN", "BROKER"),)

    async def get_portfolio(self, account_id: str):
        return AccountPortfolio(account_id, Money(Decimal("100"), "RUB"), None, None, None)


@pytest.fixture
def setup_context(monkeypatch):
    monkeypatch.setattr("moex_sentinel.composition.TInvestPortfolioAdapter", SandboxPortfolio)
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield build_application_usecases(factory), UserBrokerRepository(factory)
    finally:
        engine.dispose()


def broker_draft(*, enabled=True, account_id=None):
    return BrokerDraft(
        display_name="Sandbox setup",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=enabled,
        fields=(
            BrokerField(name="token", value="synthetic-token"),
            BrokerField(name="fqdn", value="sandbox-invest-public-api.tbank.ru:443"),
        ),
        is_test=True,
        account_id=account_id,
    )


def test_draft_can_discover_account_and_activate_through_composed_usecases(setup_context):
    usecases, repository = setup_context
    created = usecases.save_broker_settings.execute(None, broker_draft())
    assert repository.get(created.id).state is UserBrokerState.DRAFT
    assert created.enabled is False

    connection = asyncio.run(usecases.check_broker_connection.execute(created.id))
    accounts = asyncio.run(usecases.view_broker_accounts.execute(created.id))

    assert connection.available is True
    assert connection.accounts_count == 1
    assert accounts.errors == ()
    assert len(accounts.accounts) == 1
    assert accounts.accounts[0].account.account_id == "account-1"
    assert accounts.accounts[0].account.status == "ACCOUNT_STATUS_OPEN"
    assert repository.get(created.id).state is UserBrokerState.DRAFT

    activated = usecases.save_broker_settings.execute(created.id, broker_draft(account_id="account-1"))
    assert activated.enabled is True
    assert repository.get(created.id).state is UserBrokerState.ACTIVE


@pytest.mark.parametrize("account_id", [None, "account-1"])
def test_disabled_broker_cannot_check_or_discover_accounts(setup_context, account_id):
    usecases, repository = setup_context
    created = usecases.save_broker_settings.execute(None, broker_draft(enabled=False, account_id=account_id))
    assert repository.get(created.id).state is UserBrokerState.DISABLED

    with pytest.raises(UseCaseError) as raised:
        asyncio.run(usecases.check_broker_connection.execute(created.id))
    assert raised.value.code == "BROKER_CONFIGURATION"
    accounts = asyncio.run(usecases.view_broker_accounts.execute(created.id))
    assert accounts.accounts == ()
    assert len(accounts.errors) == 1
    assert accounts.errors[0].code == "BROKER_CONFIGURATION"


def test_draft_remains_unavailable_for_market_and_worker_execution(setup_context):
    usecases, _ = setup_context
    created = usecases.save_broker_settings.execute(None, broker_draft())

    with pytest.raises(UseCaseError):
        asyncio.run(usecases.synchronize_broker_instruments.execute(created.id))
    with pytest.raises(UseCaseError):
        usecases.view_automaton_broker_connection.execute(created.id)
    assert asyncio.run(usecases.view_portfolio_summary.execute()).accounts == ()
