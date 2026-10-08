"""Missing persisted broker scopes retain a stable application error."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from moex_sentinel.domain.market_data import CandleInterval
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
from moex_sentinel.services.connections import BrokerConnectionService
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.services.market_data import BrokerMarketDataService
from moex_sentinel.services.portfolio import PortfolioAggregationService
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.usecases.automations import CreateTradingAutomationUsecase
from moex_sentinel.usecases.automaton_brokers import (
    ViewAutomatonBrokerConnectionUsecase,
    ViewAutomatonBrokerScopeUsecase,
)
from moex_sentinel.usecases.connections import CheckBrokerConnectionUsecase
from moex_sentinel.usecases.errors import UseCaseError
from moex_sentinel.usecases.instruments import (
    SetInstrumentSelectionUsecase,
    SynchronizeBrokerInstrumentsUsecase,
    ViewBrokerInstrumentsUsecase,
    ViewInstrumentDetailsUsecase,
)
from moex_sentinel.usecases.market_data import (
    SearchMarketInstrumentsUsecase,
    ViewHistoricCandlesUsecase,
    ViewMarketInstrumentUsecase,
)
from moex_sentinel.usecases.portfolio import ViewBrokerAccountsUsecase

MISSING = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 6, tzinfo=UTC)


class Environment:
    def view(self):
        return SimpleNamespace(active_environment="TEST")


def forbidden_adapter(_broker):
    raise AssertionError("Missing broker lookup must happen before adapter construction")


@pytest.fixture
def missing_broker_usecases():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    brokers = UserBrokerRepository(factory)
    catalog = ReferenceCatalogRepository(factory)
    environment = Environment()
    connection = BrokerConnectionService(brokers, forbidden_adapter, environment)
    portfolio = PortfolioAggregationService(brokers, forbidden_adapter, environment)
    market = BrokerMarketDataService(brokers, forbidden_adapter, environment)
    instruments = InstrumentCatalogService(brokers, catalog, forbidden_adapter, environment)
    automaton = AutomatonBrokerService(brokers, environment=environment)
    automation = AutomationService(
        AutomationRepository(factory), access_mode="TRADE", environment=environment, brokers=brokers
    )
    try:
        yield {
            "check": lambda: CheckBrokerConnectionUsecase(connection).execute(MISSING),
            "accounts": lambda: ViewBrokerAccountsUsecase(portfolio).execute(MISSING),
            "search": lambda: SearchMarketInstrumentsUsecase(market).execute(MISSING, "SBER", 10),
            "market": lambda: ViewMarketInstrumentUsecase(market).execute(MISSING, "instrument"),
            "candles": lambda: ViewHistoricCandlesUsecase(market).execute(
                MISSING, "instrument", NOW, NOW + timedelta(hours=1), CandleInterval.MIN_1
            ),
            "synchronize": lambda: SynchronizeBrokerInstrumentsUsecase(instruments).execute(MISSING),
            "catalog": lambda: ViewBrokerInstrumentsUsecase(instruments).execute(
                MISSING, None, False, False, None, None, "RUB"
            ),
            "details": lambda: ViewInstrumentDetailsUsecase(instruments).execute(MISSING, "instrument"),
            "selection": lambda: SetInstrumentSelectionUsecase(instruments).execute(MISSING, "instrument", True),
            "connection": lambda: ViewAutomatonBrokerConnectionUsecase(automaton).execute(MISSING),
            "scope": lambda: ViewAutomatonBrokerScopeUsecase(automaton).execute(MISSING),
            "create": lambda: CreateTradingAutomationUsecase(automation).execute(MISSING, "account", "instrument"),
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "operation",
    [
        "check",
        "accounts",
        "search",
        "market",
        "candles",
        "synchronize",
        "catalog",
        "details",
        "selection",
        "connection",
        "scope",
        "create",
    ],
)
def test_actual_repository_missing_broker_is_application_not_found(missing_broker_usecases, operation):
    def execute():
        result = missing_broker_usecases[operation]()
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    with pytest.raises(UseCaseError) as caught:
        execute()
    assert caught.value.code == "BROKER_NOT_FOUND"


def test_invalid_market_query_precedes_missing_broker():
    usecase = SearchMarketInstrumentsUsecase(BrokerMarketDataService(SimpleNamespace(), forbidden_adapter))
    with pytest.raises(UseCaseError) as caught:
        asyncio.run(usecase.execute(MISSING, "S", 10))
    assert caught.value.code == "INVALID_MARKET_QUERY"


def test_read_only_create_precedes_missing_broker():
    service = AutomationService(SimpleNamespace(), brokers=SimpleNamespace(), environment=Environment())
    with pytest.raises(ValueError, match="READ_ONLY"):
        asyncio.run(CreateTradingAutomationUsecase(service).execute(MISSING, "account", "instrument"))
