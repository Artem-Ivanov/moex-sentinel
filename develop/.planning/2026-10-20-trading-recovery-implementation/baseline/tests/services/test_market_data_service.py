import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moex_sentinel.domain.brokers import Broker, BrokerField
from moex_sentinel.domain.environment import EnvironmentState
from moex_sentinel.domain.market_data import (
    CandleInterval,
    HistoricCandle,
    LastPrice,
    MarketInstrument,
)
from moex_sentinel.services.environment import EnvironmentMismatchError
from moex_sentinel.services.market_data import BrokerMarketDataService


def broker(*, enabled: bool = True, is_test: bool = True) -> Broker:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    return Broker(
        id="broker-1",
        display_name="Sandbox",
        provider_code="TINVEST",
        environment_code="SANDBOX",
        adapter_code="TINVEST_SANDBOX",
        enabled=enabled,
        fields=(BrokerField(name="token", value="synthetic-token"),),
        created_at=now,
        updated_at=now,
        is_test=is_test,
    )


class Repository:
    def __init__(self, record: Broker) -> None:
        self.record = record

    def get(self, broker_id: str) -> Broker:
        assert broker_id == self.record.id
        return self.record


class Environment:
    def view(self) -> EnvironmentState:
        return EnvironmentState("TEST", True, True)


class Adapter:
    async def search_instruments(self, query: str):
        assert query == "SBER"
        return (
            MarketInstrument(
                instrument_id="uid-1",
                figi="figi-1",
                ticker="SBER",
                name="Sber",
                class_code="TQBR",
                instrument_type="SHARE",
                currency=None,
                lot=10,
                min_price_increment=Decimal("0.01"),
                api_trade_available=True,
            ),
            MarketInstrument(
                instrument_id="uid-2",
                figi="figi-2",
                ticker="OLD",
                name="Old",
                class_code="TQBR",
                instrument_type="SHARE",
                currency=None,
                lot=1,
                min_price_increment=Decimal("0.01"),
                api_trade_available=False,
            ),
        )

    async def get_instrument(self, instrument_id: str):
        return MarketInstrument(
            instrument_id=instrument_id,
            figi="figi-1",
            ticker="SBER",
            name="Sber",
            class_code="TQBR",
            instrument_type="SHARE",
            currency="RUB",
            lot=10,
            min_price_increment=Decimal("0.01"),
            api_trade_available=True,
        )

    async def get_last_prices(self, instrument_ids: tuple[str, ...]):
        assert instrument_ids == ("uid-1",)
        return (
            LastPrice(instrument_id="uid-1", price=Decimal("312.45"), captured_at=datetime(2026, 8, 5, 10, tzinfo=UTC)),
        )

    async def get_candles(self, instrument_id, start, end, interval):
        return (
            HistoricCandle(
                instrument_id=instrument_id,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=10,
                started_at=start,
                is_complete=True,
            ),
            HistoricCandle(
                instrument_id=instrument_id,
                open=Decimal("2"),
                high=Decimal("3"),
                low=Decimal("2"),
                close=Decimal("3"),
                volume=5,
                started_at=end,
                is_complete=False,
            ),
        )


def service(record: Broker | None = None) -> BrokerMarketDataService:
    return BrokerMarketDataService(
        Repository(record or broker()),
        lambda _broker: Adapter(),
        Environment(),
    )


def test_search_returns_only_tradeable_limited_snapshots_with_joined_price() -> None:
    result = asyncio.run(service().search("broker-1", "SBER", limit=1))

    assert len(result) == 1
    assert result[0].instrument.ticker == "SBER"
    assert result[0].last_price is not None
    assert result[0].last_price.price == Decimal("312.45")


def test_candles_exclude_incomplete_current_interval() -> None:
    start = datetime(2026, 8, 5, 9, tzinfo=UTC)
    end = datetime(2026, 8, 5, 10, tzinfo=UTC)

    result = asyncio.run(service().candles("broker-1", "uid-1", start, end, CandleInterval.MIN_5))

    assert len(result) == 1
    assert result[0].started_at == start


@pytest.mark.parametrize(
    ("record", "error"),
    [(broker(enabled=False), ValueError), (broker(is_test=False), EnvironmentMismatchError)],
)
def test_market_data_rejects_unavailable_broker_context(record: Broker, error: type[Exception]) -> None:
    with pytest.raises(error):
        asyncio.run(service(record).search("broker-1", "SBER", 10))
