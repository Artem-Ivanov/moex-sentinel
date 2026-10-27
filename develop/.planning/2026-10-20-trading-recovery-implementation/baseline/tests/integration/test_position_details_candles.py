"""Position details resolve Core instrument IDs before requesting broker candles."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel import composition
from moex_sentinel.domain.instrument_catalog import CatalogInstrumentNotFoundError
from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle
from moex_sentinel.domain.portfolio import OperationsPage
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.services.portfolio import PortfolioAggregationService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base, TradingAutomationModel
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from tests.storage.trading_facts_helpers import instrument_model, user_broker_model

BROKER_ID = "00000000-0000-4000-8000-000000000701"
INTERNAL_ID = "00000000-0000-4000-8000-000000000702"
AUTOMATION_ID = "00000000-0000-4000-8000-000000000703"
EXTERNAL_ID = "00000000-0000-4000-8000-000000000704"


class Market:
    """The external market recognises only its broker UID."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, datetime, datetime, CandleInterval]] = []

    async def get_candles(self, instrument_id, start, end, interval):
        self.requests.append((instrument_id, start, end, interval))
        if instrument_id != EXTERNAL_ID:
            raise ValueError("Unknown broker instrument UID")
        return tuple(
            HistoricCandle(
                instrument_id=EXTERNAL_ID,
                open=Decimal("550"),
                high=Decimal("552"),
                low=Decimal("549"),
                close=Decimal("551"),
                volume=10,
                started_at=end.replace(second=0, microsecond=0) - timedelta(minutes=offset),
                is_complete=complete,
            )
            for offset, complete in ((1, True), (0, False))
        )


class Portfolio:
    def __init__(self):
        self.requests = []

    async def get_operations(self, account_id, cursor, limit, instrument_id):
        self.requests.append((account_id, cursor, limit, instrument_id))
        if instrument_id != EXTERNAL_ID:
            raise ValueError("Unknown broker instrument UID")
        return OperationsPage(items=(), next_cursor=None)


@pytest.fixture
def application(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[composition.ApplicationUsecases, Market, sessionmaker[Session], Portfolio]]:
    """Build real composition and scoped catalog with synthetic external adapters."""
    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        now = datetime.now(UTC)
        with factory.begin() as session:
            instrument = instrument_model(INTERNAL_ID, BROKER_ID)
            instrument.external_instrument_id = EXTERNAL_ID
            broker = user_broker_model(BROKER_ID, "synthetic-account")
            broker.api_slug = "TINVEST_SANDBOX"
            session.add(broker)
            session.flush()
            session.add(instrument)
            session.flush()
            session.add(
                TradingAutomationModel(
                    id=AUTOMATION_ID,
                    user_broker_id=BROKER_ID,
                    instrument_id=INTERNAL_ID,
                    state="IN_WORK",
                    revision=1,
                    last_sequence_number=0,
                    resume_requested=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        market = Market()
        portfolio = Portfolio()
        monkeypatch.setattr(composition, "TInvestMarketDataAdapter", lambda token, target: market)
        monkeypatch.setattr(composition, "TInvestPortfolioAdapter", lambda token, target: portfolio)
        yield composition.build_application_usecases(factory), market, factory, portfolio
    finally:
        engine.dispose()


def test_position_details_loads_completed_two_hour_candles_from_broker_uid(application) -> None:
    usecases, market, _factory, _portfolio = application
    before = datetime.now(UTC)

    details = asyncio.run(usecases.view_trading_automation_details.execute(AUTOMATION_ID))

    assert details.automation.instrument_id == INTERNAL_ID
    assert len(details.candles) == 1
    assert details.candles[0].instrument_id == EXTERNAL_ID
    assert details.candles[0].close == Decimal("551")
    assert details.candles[0].is_complete is True
    assert details.errors == ()
    [(instrument_id, start, end, interval)] = market.requests
    assert instrument_id == EXTERNAL_ID
    assert end - start == timedelta(hours=2)
    assert before <= end <= datetime.now(UTC)
    assert interval is CandleInterval.MIN_1


def test_generic_market_candles_still_accepts_external_id(application) -> None:
    usecases, market, _factory, _portfolio = application
    end = datetime(2026, 9, 15, 12, tzinfo=UTC)
    start = end - timedelta(hours=2)

    candles = asyncio.run(
        usecases.view_historic_candles.execute(
            BROKER_ID,
            EXTERNAL_ID,
            start,
            end,
            CandleInterval.MIN_1,
        )
    )

    assert len(candles) == 1
    assert candles[0].close == Decimal("551")
    assert market.requests == [(EXTERNAL_ID, start, end, CandleInterval.MIN_1)]


def test_catalog_candles_rejects_another_brokers_instrument_without_market_request(application) -> None:
    _usecases, market, factory, _portfolio = application
    other_broker_id = "00000000-0000-4000-8000-000000000705"
    with factory.begin() as session:
        broker = user_broker_model(other_broker_id, "another-account")
        broker.api_slug = "TINVEST_SANDBOX"
        session.add(broker)
    catalog = InstrumentCatalogService(
        UserBrokerRepository(factory),
        ReferenceCatalogRepository(factory),
        lambda _broker: market,
    )
    end = datetime(2026, 9, 15, 12, tzinfo=UTC)

    with pytest.raises(CatalogInstrumentNotFoundError):
        asyncio.run(catalog.candles(other_broker_id, INTERNAL_ID, end - timedelta(hours=2), end, CandleInterval.MIN_1))

    assert market.requests == []


def test_position_details_operations_use_the_scoped_external_instrument_id(application):
    usecases, _market, _factory, portfolio = application

    details = asyncio.run(usecases.view_trading_automation_details.execute(AUTOMATION_ID))

    assert details.errors == ()
    assert len(portfolio.requests) == 1
    assert portfolio.requests[0][0] == "synthetic-account"
    assert portfolio.requests[0][3] == EXTERNAL_ID


@pytest.mark.parametrize("foreign_record", [pytest.param(False, id="missing"), pytest.param(True, id="cross-scope")])
def test_position_operations_reject_unscoped_catalog_id_before_broker_io(application, foreign_record):
    _usecases, _market, factory, portfolio = application
    unknown_id = "00000000-0000-4000-8000-000000000799"
    if foreign_record:
        other_broker_id = "00000000-0000-4000-8000-000000000798"
        with factory.begin() as session:
            session.add(user_broker_model(other_broker_id, "foreign-account"))
            session.flush()
            session.add(instrument_model(unknown_id, other_broker_id))
    service = PortfolioAggregationService(
        UserBrokerRepository(factory),
        lambda _broker: portfolio,
        instruments=ReferenceCatalogRepository(factory),
    )

    with pytest.raises(CatalogInstrumentNotFoundError):
        asyncio.run(service.view_position_operations(BROKER_ID, "synthetic-account", unknown_id, 50))

    assert portfolio.requests == []
